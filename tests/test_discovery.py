import base64
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from attraccess_tunnel.discovery import (
    ADDRESS_MARKER, CONTAINER_MARKER, INSPECT_TEMPLATE, NPM_QUERY, PROXY_MARKER, build_discovery, choose_proxy,
    hostname, parse_containers, parse_host_addresses, parse_proxy,
)


def container(service, ports=None, network="bridge", exposed=None):
    return {"id": "a" * 64, "name": service, "image": "registry.example/release@sha256:hash",
            "labels": {"io.balena.service-name": service}, "network": network,
            "ports": ports or {}, "exposed": exposed or {}}


def binding(port):
    return [{"HostIp": "0.0.0.0", "HostPort": str(port)}, {"HostIp": "::", "HostPort": str(port)}]


class DiscoveryTests(unittest.TestCase):
    def test_live_attraccess_ports_and_all_host_types(self):
        proxy = container("proxy", {f"{port}/tcp": binding(port) for port in (80, 81, 443, 8883, 8080, 8081)})
        proxy["ports"]["1883/tcp"] = binding(2883)
        rabbit = container("rabbitmq", {"1883/tcp": binding(1883), "15675/tcp": binding(15675)})
        config = container("config-ui", network="host", exposed={"5380/tcp": {}, "53/tcp": {}, "53/udp": {}})
        internal = container("valkey", exposed={"6379/tcp": {}})
        data = {"hosts": [
            {"type": "proxy_host", "names": ["App.Example", "*.example", "büro.example"], "enabled": True},
            {"type": "redirection_host", "names": ["redirect.example"], "enabled": True},
            {"type": "dead_host", "names": ["disabled.example"], "enabled": False},
        ], "streams": [{"incoming_port": 1883, "tcp_forwarding": 1, "udp_forwarding": 0},
                       {"incoming_port": 9999, "tcp_forwarding": 1, "udp_forwarding": 1}]}
        result = build_discovery([proxy, rabbit, config, internal], data, ["concrete.example"])
        self.assertEqual(result.ports, [53, 80, 81, 443, 1883, 2883, 5380, 8080, 8081, 8883, 15675])
        self.assertEqual(result.domains, ["app.example", "concrete.example", "disabled.example", "redirect.example", "xn--bro-hoa.example"])
        self.assertTrue(any("wildcard" in warning for warning in result.warnings))
        self.assertTrue(any("Disabled" in warning for warning in result.warnings))
        self.assertTrue(any("not published" in warning for warning in result.warnings))

    def test_routes_match_aliases_ips_tls_custom_paths_and_remapped_bindings(self):
        proxy = container("proxy", {"80/tcp": binding(8088), "443/tcp": binding(8443), "1883/tcp": binding(2883)})
        proxy["networks"] = {"default": {"Gateway": "172.18.0.1"}}
        app = container("attraccess", exposed={"3000/tcp": {}})
        app["id"] = "b" * 64
        app["networks"] = {"default": {"Aliases": ["app-alias"], "IPAddress": "172.18.0.4"}}
        rabbit = container("rabbitmq")
        rabbit["id"] = "c" * 64
        data = {"hosts": [
            {"type": "proxy_host", "names": ["app.example", "*.apps.example"], "enabled": True,
             "certificate_id": 7, "forward_scheme": "http", "forward_host": "app-alias", "forward_port": 3000,
             "locations": [{"path": "/metrics", "forward_scheme": "http", "forward_host": "172.18.0.4", "forward_port": 9000}]},
            {"type": "proxy_host", "names": ["plain.example"], "enabled": True,
             "certificate_id": 0, "forward_scheme": "https", "forward_host": "external.example", "forward_port": 9443},
            {"type": "redirection_host", "names": ["redirect.example"], "enabled": True,
             "forward_scheme": "https", "forward_domain_name": "elsewhere.example", "forward_http_code": 301},
            {"type": "dead_host", "names": ["dead.example"], "enabled": False, "certificate_id": 0},
        ], "streams": [{"incoming_port": 1883, "tcp_forwarding": 1, "udp_forwarding": 0,
                        "forwarding_host": "rabbitmq", "forwarding_port": 1883}]}
        result = build_discovery([proxy, app, rabbit], data, ["device.apps.example"], proxy)
        primary = next(r for r in result.routes if r.domain == "app.example" and r.path == "/")
        self.assertEqual(primary.url, "https://app.example:8443")
        self.assertEqual((primary.proxy_service, primary.proxy_port), ("proxy", 443))
        self.assertEqual((primary.target.service, primary.target.port, primary.target.scheme), ("attraccess", 3000, "http"))
        location = next(r for r in result.routes if r.domain == "app.example" and r.path == "/metrics")
        self.assertEqual(location.url, "https://app.example:8443/metrics")
        self.assertEqual(location.target.service, "attraccess")
        wildcard = next(r for r in result.routes if r.domain == "device.apps.example" and r.path == "/")
        self.assertEqual(wildcard.target.service, "attraccess")
        plain = next(r for r in result.routes if r.domain == "plain.example")
        self.assertEqual(plain.url, "http://plain.example:8088")
        self.assertIsNone(plain.target.service)
        redirect = next(r for r in result.routes if r.domain == "redirect.example")
        self.assertEqual(redirect.redirect, "https://elsewhere.example")
        self.assertIsNone(redirect.target)
        dead = next(r for r in result.routes if r.domain == "dead.example")
        self.assertFalse(dead.enabled)
        self.assertIsNone(dead.target)
        mqtt = next(b for b in result.bindings if b.host_port == 2883)
        self.assertEqual((mqtt.service, mqtt.container_port), ("proxy", 1883))
        self.assertEqual((mqtt.target.service, mqtt.target.port), ("rabbitmq", 1883))
        self.assertEqual(len(result.bindings), 3)  # IPv4 / IPv6 addresses describe the same binding.
        self.assertNotIn(3000, result.ports)

    def test_gateway_targets_resolve_host_ports_and_missing_tls_is_reported(self):
        proxy = container("proxy", {"80/tcp": binding(80)})
        proxy["networks"] = {"default": {"Gateway": "172.18.0.1"}}
        config = container("config-ui", network="host", exposed={"5380/tcp": {}})
        config["id"] = "d" * 64
        data = {"hosts": [{"names": ["config.example"], "type": "proxy_host", "enabled": True,
                          "certificate_id": 1, "forward_host": "172.18.0.1", "forward_port": 5380}], "streams": []}
        result = build_discovery([proxy, config], data, proxy_container=proxy)
        self.assertEqual(result.routes[0].target.service, "config-ui")
        self.assertIsNone(result.routes[0].url)
        self.assertTrue(any("HTTPS port 443" in w for w in result.warnings))
        # In bridge mode, localhost points inside the proxy container, not at the device.
        data["hosts"][0]["forward_host"] = "localhost"
        result = build_discovery([proxy, config], data, proxy_container=proxy)
        self.assertEqual(result.routes[0].target.service, "proxy")
        proxy["network"] = "host"
        proxy["exposed"] = {"443/tcp": {}}
        result = build_discovery([proxy, config], data, proxy_container=proxy)
        self.assertEqual(result.routes[0].target.service, "config-ui")
        self.assertEqual(result.routes[0].url, "https://config.example")

    def test_device_lan_address_maps_to_container_without_changing_configured_port(self):
        proxy = container("proxy", {"443/tcp": binding(443)})
        rabbit = container("rabbitmq", {"15672/tcp": binding(15670)})
        rabbit["id"] = "c" * 64
        data = {"hosts": [{"names": ["rabbit.example"], "type": "proxy_host", "enabled": True,
                          "certificate_id": 1, "forward_host": "192.168.2.10", "forward_port": 15670}], "streams": []}
        result = build_discovery([proxy, rabbit], data, proxy_container=proxy, host_addresses=["192.168.2.10"])
        target = result.routes[0].target
        self.assertEqual(target.label, "rabbitmq:15672")
        self.assertEqual((target.host, target.port), ("192.168.2.10", 15670))
        result = build_discovery([proxy, rabbit], data, proxy_container=proxy, host_addresses=["192.168.2.11"])
        self.assertIsNone(result.routes[0].target.service)

    def test_host_address_frames_accept_cidrs_and_ignore_shell_noise(self):
        output = f"banner\r\n{ADDRESS_MARKER}BEGIN\r\n192.168.2.10/24\r\n2001:db8::1/64\r\nnot-an-ip\r\n{ADDRESS_MARKER}END\r\nprompt\r\n"
        self.assertEqual(parse_host_addresses(output), ["192.168.2.10", "2001:db8::1"])
        self.assertEqual(parse_host_addresses("no frame"), [])

    def test_custom_host_network_proxy_includes_streams(self):
        proxy = container("custom-proxy", network="host", exposed={"80/tcp": {}})
        result = build_discovery([proxy], {"hosts": [], "streams": [
            {"incoming_port": 12345, "tcp_forwarding": 1, "udp_forwarding": 0}
        ]}, proxy_container=proxy)
        self.assertEqual(result.ports, [80, 12345])

    def test_proxy_label_works_with_balena_registry_image_names(self):
        proxy = container("my-proxy")
        self.assertIs(choose_proxy([proxy], "my-proxy"), proxy)
        with self.assertRaises(RuntimeError):
            choose_proxy([proxy, proxy], "my-proxy")
        with self.assertRaises(RuntimeError):
            choose_proxy([proxy], "proxy")

    def test_protocol_frames_ignore_shell_noise(self):
        expected = container("proxy")
        output = f"banner\r\n{CONTAINER_MARKER}BEGIN\r\n{json.dumps(expected)}\r\n{CONTAINER_MARKER}END\r\nprompt\r\n"
        self.assertEqual(parse_containers(output), [expected])
        payload = {"hosts": [], "streams": []}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        self.assertEqual(parse_proxy(f"Sqlite configuration\n{PROXY_MARKER}{encoded}\r\n"), payload)
        with self.assertRaises(RuntimeError):
            parse_containers("missing frames")
        with self.assertRaises(RuntimeError):
            parse_proxy("query failed")

    def test_hostname_validation(self):
        self.assertEqual(hostname("BÜRO.Example."), "xn--bro-hoa.example")
        for value in ("*.example", "bad_name.example", "bad\nexample", "-bad.example", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                hostname(value)

    @unittest.skipUnless(shutil.which("go"), "Go is only needed to exercise the engine's template evaluator")
    def test_engine_template_handles_containers_without_exposed_ports(self):
        fixture = {
            "Id": "a" * 64, "Name": "without-exposed-ports",
            "Config": {"Image": "registry.example/release", "Labels": {"io.balena.service-name": "config-ui"}},
            "HostConfig": {"NetworkMode": "host"}, "NetworkSettings": {"Ports": None},
        }
        renderer = Path(__file__).parent / "fixtures" / "render_inspect.go"
        result = subprocess.run(["go", "run", str(renderer), INSPECT_TEMPLATE],
                                input=json.dumps(fixture), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = parse_containers(f"{CONTAINER_MARKER}BEGIN\n{result.stdout}\n{CONTAINER_MARKER}END\n")
        self.assertEqual(parsed[0]["exposed"], None)
        self.assertEqual(parsed[0]["ports"], None)

    @unittest.skipUnless(shutil.which("node"), "Node is only needed to test the remote NPM script")
    def test_database_query_supports_cjs_and_esm_without_mutations(self):
        for esm in (False, True):
            with self.subTest(esm=esm), tempfile.TemporaryDirectory() as directory:
                db = Path(directory) / ("db.mjs" if esm else "db.cjs")
                source = """
const db = (table) => ({
  where(field, value) {
    if (field !== 'is_deleted' || value !== 0) throw Error('unsafe filter');
    return this;
  },
  async select(...columns) {
    const expected = ['domain_names', 'enabled', 'certificate_id', 'ssl_forced'];
    if (table === 'proxy_host') expected.push('forward_scheme', 'forward_host', 'forward_port', 'locations');
    if (table === 'redirection_host') expected.push('forward_scheme', 'forward_domain_name', 'forward_http_code');
    if (table === 'stream') {
      if (columns.join() !== 'incoming_port,tcp_forwarding,udp_forwarding,forwarding_host,forwarding_port') throw Error('unexpected stream query');
      return [{incoming_port: 2883, tcp_forwarding: 1, udp_forwarding: 0, forwarding_host: 'rabbitmq', forwarding_port: 1883}];
    }
    if (columns.join() !== expected.join()) throw Error('unexpected host query');
    return [{domain_names: table === 'proxy_host' ? '["app.example"]' : ['other.example'], enabled: 1,
             certificate_id: 3, forward_scheme: 'http', forward_host: 'attraccess', forward_port: 3000,
             locations: '[{"path":"/api","forward_host":"api","forward_port":8080}]'}];
  }
});
db.client = {}; db.destroy = async () => {};
"""
                source += "export default () => db;" if esm else "module.exports = db;"
                db.write_text(source)
                query = NPM_QUERY.replace("'/app/db.js'", json.dumps(str(db)))
                result = subprocess.run(["node", "-e", query], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = parse_proxy(result.stdout)
                self.assertEqual(len(payload["hosts"]), 3)
                self.assertEqual(payload["hosts"][0]["names"], ["app.example"])
                self.assertEqual(payload["hosts"][0]["forward_host"], "attraccess")
                self.assertEqual(payload["hosts"][0]["locations"][0]["forward_port"], 8080)
                self.assertEqual(payload["streams"][0]["forwarding_host"], "rabbitmq")


if __name__ == "__main__":
    unittest.main()
