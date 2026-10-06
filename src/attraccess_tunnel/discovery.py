"""Read the device's live container bindings and Nginx Proxy Manager database."""

from dataclasses import dataclass, field, replace
import base64
import ipaddress
import json
import re
import shlex
from urllib.parse import quote

from .processes import remote_command


CONTAINER_MARKER = "__ATTRACCESS_CONTAINERS__"
PROXY_MARKER = "__ATTRACCESS_PROXY__"
ADDRESS_MARKER = "__ATTRACCESS_ADDRESSES__"
# Engine templates use strict key access. Containers without advertised ports
# omit ExposedPorts; map lookups must return null rather than abort inspection.
INSPECT_TEMPLATE = (
    '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Config.Image}},'
    '"labels":{{json .Config.Labels}},"network":{{json .HostConfig.NetworkMode}},'
    '"hostname":{{json (index .Config "Hostname")}},'
    '"networks":{{json (index .NetworkSettings "Networks")}},'
    '"ports":{{json (index .NetworkSettings "Ports")}},'
    '"exposed":{{json (index .Config "ExposedPorts")}}}'
)

# Import NPM's own database adapter, supporting both older CommonJS versions and
# the ESM factory in 2.16+. Use only SELECT queries; never request admin credentials.
NPM_QUERY = r"""
(async () => {
  const imported = await import('/app/db.js');
  const db = imported.default.client ? imported.default : imported.default();
  try {
    const hosts = [];
    for (const table of ['proxy_host', 'redirection_host', 'dead_host']) {
      const columns = ['domain_names', 'enabled', 'certificate_id', 'ssl_forced'];
      if (table === 'proxy_host') columns.push('forward_scheme', 'forward_host', 'forward_port', 'locations');
      if (table === 'redirection_host') columns.push('forward_scheme', 'forward_domain_name', 'forward_http_code');
      const rows = await db(table).where('is_deleted', 0).select(...columns);
      for (const row of rows) {
        const names = typeof row.domain_names === 'string' ? JSON.parse(row.domain_names) : row.domain_names;
        const locations = typeof row.locations === 'string' ? JSON.parse(row.locations) : row.locations;
        hosts.push({...row, domain_names: undefined, type: table, names, locations, enabled: !!row.enabled});
      }
    }
    const streams = await db('stream').where('is_deleted', 0)
      .select('incoming_port', 'tcp_forwarding', 'udp_forwarding', 'forwarding_host', 'forwarding_port');
    const result = Buffer.from(JSON.stringify({hosts, streams})).toString('base64');
    console.log('__ATTRACCESS_PROXY__' + result);
  } finally { await db.destroy(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
"""


@dataclass
class Discovery:
    domains: list[str]
    ports: list[int]
    warnings: list[str]
    routes: list["Route"] = field(default_factory=list)
    bindings: list["PortBinding"] = field(default_factory=list)


@dataclass(frozen=True)
class Destination:
    host: str
    port: int
    service: str | None = None
    scheme: str = "tcp"
    container_port: int | None = None

    @property
    def label(self):
        host = self.service or self.host
        port = self.container_port if self.service and self.container_port is not None else self.port
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


@dataclass(frozen=True)
class PortBinding:
    host_port: int
    container_port: int
    service: str
    container_id: str
    network: str
    target: Destination | None = None


@dataclass(frozen=True)
class Route:
    domain: str
    scheme: str
    host_port: int | None
    proxy_service: str
    proxy_port: int
    kind: str
    enabled: bool
    target: Destination | None = None
    path: str = "/"
    redirect: str | None = None

    @property
    def url(self):
        if self.host_port is None or not self.path.startswith("/"):
            return None
        port = "" if self.host_port == {"http": 80, "https": 443}[self.scheme] else f":{self.host_port}"
        path = "" if self.path == "/" else quote(self.path, safe="/%-._~")
        return f"{self.scheme}://{self.domain}{port}{path}"


def service_name(container):
    return (container.get("labels") or {}).get("io.balena.service-name") or container.get("name", "container").lstrip("/")


def destination(host, port, containers, bindings, proxy_container, scheme="tcp", host_addresses=()):
    """Resolve configured names/IPs without guessing which external host owns a port."""
    if not host or port is None:
        return None
    host, port = str(host).strip(), valid_port(port)
    matches = []
    for container in containers:
        aliases = {service_name(container), container.get("name", "").lstrip("/"),
                   container.get("hostname"), container.get("id"), container.get("id", "")[:12]}
        for network in (container.get("networks") or {}).values():
            aliases.update(network.get("Aliases") or [])
            aliases.update((network.get("IPAddress"), network.get("GlobalIPv6Address")))
        if host in aliases:
            matches.append(service_name(container))
    if proxy_container is not None:
        gateways = {n.get("Gateway") for n in (proxy_container.get("networks") or {}).values()}
        if host in ("localhost", "127.0.0.1", "::1"):
            if proxy_container.get("network") != "host":
                return Destination(host, port, service_name(proxy_container), scheme)
        # Device interface addresses and bridge gateways refer to host ports.
        if host in gateways or host in host_addresses or (
            host in ("localhost", "127.0.0.1", "::1") and proxy_container.get("network") == "host"
        ):
            bound = [b for b in bindings if b.host_port == port]
            if len({(b.service, b.container_port) for b in bound}) == 1:
                return Destination(host, port, bound[0].service, scheme, bound[0].container_port)
    services = set(matches)
    return Destination(host, port, next(iter(services)) if len(services) == 1 else None, scheme)


def build_routes(containers, proxy, domains, bindings, proxy_container, host_addresses=()):
    routes = []
    if proxy_container is None:
        return routes
    service = service_name(proxy_container)
    ingress = {port: sorted({b.host_port for b in bindings if b.container_id == proxy_container["id"] and b.container_port == port})
               for port in (80, 443)}
    hosts = list(proxy["hosts"])
    # Extra concrete names inherit their matching wildcard's route, if any.
    configured = set()
    for host in hosts:
        for name in host["names"]:
            try:
                configured.add(hostname(name))
            except (ValueError, UnicodeError):
                pass
    for domain in sorted(set(domains) - configured):
        candidates = [host for host in proxy["hosts"] if any(
            name.startswith(("*.", ".")) and domain.endswith(name.lstrip("*")) for name in host["names"]
        )]
        if candidates:
            # Nginx chooses the longest matching wildcard suffix.
            host = max(candidates, key=lambda h: max(len(n) for n in h["names"] if n.startswith(("*.", ".")) and domain.endswith(n.lstrip("*"))))
            hosts.append({**host, "names": [domain]})
        else:
            hosts.append({"names": [domain], "type": "extra", "enabled": True})
    for host in hosts:
        tls = int(host.get("certificate_id") or 0) > 0
        scheme, proxy_port = ("https", 443) if tls else ("http", 80)
        ports = ingress[proxy_port] or [None]
        kind = host.get("type", "proxy_host")
        target = destination(host.get("forward_host"), host.get("forward_port"), containers, bindings,
                             proxy_container, host.get("forward_scheme") or "http", host_addresses) if kind == "proxy_host" else None
        redirect = None
        if kind == "redirection_host":
            redirect = f"{host.get('forward_scheme', 'http')}://{host.get('forward_domain_name', '')}"
        locations = [("/", target)]
        if kind == "proxy_host":
            for location in host.get("locations") or []:
                locations.append((location.get("path", "/"), destination(
                    location.get("forward_host"), location.get("forward_port"), containers, bindings,
                    proxy_container, location.get("forward_scheme") or "http", host_addresses)))
        for name in host["names"]:
            try:
                name = hostname(name)
            except (ValueError, UnicodeError):
                continue
            for port in ports:
                for path, upstream in locations:
                    routes.append(Route(name, scheme, port, service, proxy_port, kind,
                                        bool(host["enabled"]), upstream, path, redirect))
    return sorted(set(routes), key=lambda r: (r.domain, r.path, r.host_port or 0, r.kind))


def hostname(value):
    value = value.rstrip(".").encode("idna").decode("ascii").lower()
    if len(value) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in value.split(".")
    ):
        raise ValueError(f"Invalid hostname: {value!r}")
    return value


def valid_port(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError(f"Invalid TCP port: {value}")
    return port


def parse_containers(output):
    lines = output.replace("\r\n", "\n").splitlines()
    try:
        start = lines.index(CONTAINER_MARKER + "BEGIN")
        end = lines.index(CONTAINER_MARKER + "END", start + 1)
        containers = [json.loads(line) for line in lines[start + 1:end] if line.strip()]
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not read running containers from balena-engine on the device.") from exc
    if not containers:
        raise RuntimeError("The selected device has no running application containers.")
    return containers


def parse_host_addresses(output):
    match = re.search(r"^" + ADDRESS_MARKER + r"BEGIN\r?\n(.*?)^" + ADDRESS_MARKER + r"END\r?$",
                      output, re.M | re.S)
    if not match:
        return []
    addresses = set()
    for value in match[1].split():
        try:
            addresses.add(str(ipaddress.ip_interface(value).ip))
        except ValueError:
            continue
    return sorted(addresses)


def choose_proxy(containers, service):
    labeled = [c for c in containers if (c.get("labels") or {}).get("io.balena.service-name") == service]
    candidates = labeled or ([c for c in containers if "nginx-proxy-manager" in c.get("image", "")] if service == "proxy" else [])
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected one running Nginx Proxy Manager service ({service!r}), found {len(candidates)}. "
            "Use --proxy-service with its balena service name."
        )
    if not re.fullmatch(r"[0-9a-f]{12,64}", candidates[0]["id"]):
        raise RuntimeError("Device returned an invalid proxy container ID.")
    return candidates[0]


def parse_proxy(output):
    match = re.search(r"^" + PROXY_MARKER + r"([A-Za-z0-9+/=]+)\r?$", output, re.M)
    if not match:
        raise RuntimeError("Could not query Nginx Proxy Manager's database. Check that its service is healthy.")
    try:
        return json.loads(base64.b64decode(match.group(1), validate=True))
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Nginx Proxy Manager returned an invalid discovery response.") from exc


def build_discovery(containers, proxy, extra_domains=(), proxy_container=None, host_addresses=()):
    domains, ports, warnings = set(), set(), set()
    bindings = set()
    if proxy_container is None:
        candidates = [c for c in containers if "nginx-proxy-manager" in c.get("image", "") or
                      (c.get("labels") or {}).get("io.balena.service-name") == "proxy"]
        if len(candidates) == 1:
            proxy_container = candidates[0]
    for host in proxy["hosts"]:
        for name in host["names"]:
            if "*" in name or name.startswith("."):
                warnings.add(f"Hosts files cannot resolve wildcard {name!r}; add concrete names with --hostname.")
                continue
            try:
                domains.add(hostname(name))
            except (ValueError, UnicodeError):
                warnings.add(f"Skipping hostname that cannot be represented in a hosts file: {name!r}")
        if not host["enabled"]:
            warnings.add("Disabled proxy hosts are mapped locally but remain disabled on the device.")
    domains.update(hostname(name) for name in extra_domains)
    for container in containers:
        name = service_name(container)
        for binding, addresses in (container.get("ports") or {}).items():
            if not addresses:
                continue
            if not binding.endswith("/tcp"):
                warnings.add(f"Skipping UDP binding {binding} ({name}); balena tunnel supports TCP only.")
                continue
            for address in addresses:
                host_port = valid_port(address["HostPort"])
                ports.add(host_port)
                bindings.add(PortBinding(host_port, valid_port(binding.split("/")[0]), name,
                                         container["id"], container.get("network", "bridge")))
                if address.get("HostIp") not in (None, "", "0.0.0.0", "::", "127.0.0.1", "::1"):
                    warnings.add(f"Port {address['HostPort']} ({name}) binds to {address['HostIp']} on the device; balena's tunnel may not reach it.")
        if container.get("network") == "host":
            for binding in (container.get("exposed") or {}):
                if binding.endswith("/tcp"):
                    port = valid_port(binding.split("/")[0])
                    ports.add(port)
                    bindings.add(PortBinding(port, port, name, container["id"], "host"))
                else:
                    warnings.add(f"Skipping host-network UDP port {binding} ({name}); balena tunnel supports TCP only.")
    for stream in proxy.get("streams", []):
        # In bridge networking, an NPM stream is reachable on the device only
        # when the container's incoming port has a host binding (already above).
        if stream["tcp_forwarding"]:
            incoming = valid_port(stream["incoming_port"])
            npm_containers = [proxy_container] if proxy_container is not None else [
                c for c in containers if "nginx-proxy-manager" in c.get("image", "") or
                (c.get("labels") or {}).get("io.balena.service-name") == "proxy"
            ]
            if any(c.get("network") == "host" for c in npm_containers):
                ports.add(incoming)
                for c in npm_containers:
                    if c.get("network") == "host":
                        bindings.add(PortBinding(incoming, incoming, service_name(c), c["id"], "host"))
            elif not any((c.get("ports") or {}).get(f"{incoming}/tcp") for c in npm_containers):
                warnings.add(f"TCP proxy stream {incoming} is not published to the device host; publish it in balena compose to tunnel it.")
        if stream["udp_forwarding"]:
            warnings.add(f"Skipping UDP proxy stream {stream['incoming_port']}; balena tunnel supports TCP only.")
    if not ports:
        raise RuntimeError("No published TCP ports were found on the selected device.")
    # Attach stream destinations to the actual host -> proxy container binding.
    for stream in proxy.get("streams", []):
        if stream["tcp_forwarding"] and proxy_container is not None:
            target = destination(stream.get("forwarding_host"), stream.get("forwarding_port"),
                                 containers, bindings, proxy_container, host_addresses=host_addresses)
            bindings = {replace(b, target=target) if b.container_id == proxy_container["id"] and
                        b.container_port == valid_port(stream["incoming_port"]) else b for b in bindings}
    routes = build_routes(containers, proxy, domains, bindings, proxy_container, host_addresses)
    for route in routes:
        if route.host_port is None:
            warnings.add(f"{route.scheme.upper()} port {route.proxy_port} ({route.proxy_service}) is not published; {route.domain} has no local {route.scheme.upper()} URL.")
    return Discovery(sorted(domains), sorted(ports), sorted(warnings), routes,
                     sorted(bindings, key=lambda b: (b.host_port, b.service, b.container_port)))


def discover(balena, uuid, service, extra_domains, timeout, tick=None):
    script = (
        "set -e\n"
        "ids=$(balena-engine ps -q --filter label=io.balena.service-name)\n"
        f"printf '%s\\n' '{CONTAINER_MARKER}BEGIN'\n"
        f"if [ -n \"$ids\" ]; then balena-engine inspect --format {shlex.quote(INSPECT_TEMPLATE)} $ids; fi\n"
        f"printf '%s\\n' '{CONTAINER_MARKER}END'\n"
        f"printf '%s\\n' '{ADDRESS_MARKER}BEGIN'\n"
        "ip -o address show 2>/dev/null | awk '{print $4}'\n"
        f"printf '%s\\n' '{ADDRESS_MARKER}END'\n"
    )
    inspected = remote_command(balena, uuid, "sh -c " + shlex.quote(script), timeout, tick=tick)
    containers = parse_containers(inspected)
    proxy = choose_proxy(containers, service)
    command = shlex.join(["balena-engine", "exec", "-w", "/app", proxy["id"], "node", "-e", NPM_QUERY])
    data = parse_proxy(remote_command(balena, uuid, command, timeout, tick=tick))
    return build_discovery(containers, data, extra_domains, proxy, parse_host_addresses(inspected))
