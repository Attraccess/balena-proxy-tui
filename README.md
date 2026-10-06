<img src="assets/icon.svg" width="64" height="64" alt="">

# balena-proxy-tui

A small Python CLI with a fullscreen terminal app for accessing an Attraccess balena device
from your local machine. Supports macOS and Linux.

## Install

The installer uses Nix when available, otherwise pipx:

```sh
curl -fsSL https://raw.githubusercontent.com/Attraccess/balena-proxy-tui/main/install.sh | sh
balena-proxy-tui
```

Use `sh -s -- --nix` or `sh -s -- --pipx` after the pipe to choose explicitly.
Add `--ref <tag-or-commit>` to pin a version. Installation runs as your normal
user and adds the executable to your Nix or pipx user profile; that profile's
`bin` directory must be on your `PATH`. For pipx, `pipx ensurepath` sets this up.

### Nix

The flake supports Apple Silicon and Intel macOS, and ARM64 and x86-64 Linux.
It includes Python, the Python libraries, balena CLI and OpenSSH. The app uses
your system's `sudo` and your existing user login and SSH keys.

```sh
# Install into your user profile (the installer also enables flakes per command).
nix --extra-experimental-features 'nix-command flakes' profile install github:Attraccess/balena-proxy-tui

# Log in using the bundled balena CLI if you haven't already.
nix run github:Attraccess/balena-proxy-tui#balena -- login
balena-proxy-tui

# Or try the app without adding it to your profile.
nix run github:Attraccess/balena-proxy-tui
```

`nix profile install` is also called
[`nix profile add`](https://nix.dev/manual/nix/stable/command-ref/new-cli/nix3-profile-add)
in newer Nix versions. To update a profile installation, run
`nix profile upgrade balena-proxy-tui`.

For a global binary managed by **NixOS or nix-darwin**, add the flake input to
your configuration's `flake.nix`:

```nix
inputs.balena-proxy-tui.url = "github:Attraccess/balena-proxy-tui";
```

Pass your `inputs` to your system modules with `specialArgs = { inherit inputs; };`,
then add this to a module:

```nix
{ inputs, pkgs, ... }: {
  environment.systemPackages = [
    inputs.balena-proxy-tui.packages.${pkgs.stdenv.hostPlatform.system}.default
  ];
}
```

Rebuild your system configuration to install `balena-proxy-tui` globally.
For **Home Manager**, pass `inputs` through `extraSpecialArgs` and use
`home.packages` instead of `environment.systemPackages`. The flake exports
`packages.<system>.balena-proxy-tui`, `packages.<system>.default` and
`overlays.default` if you prefer `pkgs.balena-proxy-tui` via an overlay.
Its own `flake.lock` pins dependencies; you can optionally set
`inputs.balena-proxy-tui.inputs.nixpkgs.follows = "nixpkgs"` to use your
configuration's package set instead.

### Python / pipx

Install Python 3.10+, [pipx](https://pipx.pypa.io/), Git,
the [balena CLI](https://github.com/balena-io/balena-cli/blob/master/INSTALL.md),
OpenSSH and `sudo`, then:

```sh
pipx install git+https://github.com/Attraccess/balena-proxy-tui.git
balena login
balena-proxy-tui
```

From a checkout, use `pipx install .`, or a development virtual environment:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/balena-proxy-tui
```

`attraccess-tunnel` remains available as a compatibility alias.

Your user must be able to run `balena device ssh <uuid>`;
this may require [adding an SSH public key to balena](https://docs.balena.io/learn/manage/ssh-access/#add-an-ssh-key-to-balenacloud).
Run this tool as your normal user so it uses your existing balena login and SSH
keys. It asks for `sudo` only for local hosts entries and listeners.
The sudo helper starts in the same terminal session as authentication so it can
use the session's cached authorization. In the fullscreen app, sudo's askpass
prompt appears as a masked password field; credentials are sent to sudo through
a private temporary socket and are never written to application files or logs.

## What it does

1. Opens one fullscreen window before setup begins. Fleet loading and selection,
   balena's device picker, discovery, tunnel setup, sudo authentication, the
   running session, errors and cleanup all stay in this window. With no target
   argument, lists your accessible fleets using
   `balena fleet list --json` and presents an arrow-key fleet menu. Select with
   Up/Down (or j/k) and Enter; Ctrl+C or Escape cancels. Then runs
   `balena device tunnel <fleet> -p ...` in a real pseudo-terminal. You get
   balena's own online-device menu, including its keyboard controls. Supply a
   fleet name/slug to skip the fleet menu, or a device UUID to skip both menus.
   The short-lived selection
   tunnel closes once balena reports the chosen UUID; all subsequent operations
   use that same device.
2. Uses `balena device ssh <uuid>` to inspect running balena application
   containers through `balena-engine`. Reads Nginx Proxy Manager's database adapter inside the
   proxy container, selecting domain names from proxy hosts, redirection hosts,
   and dead/404 hosts. No NPM admin password is needed. Older CommonJS and newer
   ESM database adapters are supported; discovery issues SELECT queries only.
3. Discovers actual published TCP **host ports**, including non-identical
   bindings such as `2883:1883`. Includes advertised TCP `expose` ports for
   host-network services such as Attraccess's config UI. Container-only `expose`
   ports are kept private. Published NPM TCP streams are included; unpublished
   streams are reported because the device host cannot receive them.
4. Opens a balena tunnel on private high-numbered loopback ports, then starts a
   privileged local helper that relays each original host port on `127.0.0.1`
   and, where available, `::1`. The helper maps discovered domains to
   `127.0.0.1` in `/etc/hosts`. Balena itself stays unprivileged.
5. Shows a fullscreen dashboard with clickable proxy URLs and their destination
   containers/ports, direct TCP bindings, stream destinations, and discovery notices.
   Keeps running until `q` or Ctrl+C. Normal exit, SIGTERM, SIGHUP, setup errors, and
   balena process exit close the tunnels/listeners and restore hosts entries.
   Once the helper is active, it also detects parent crashes or SIGKILL through
   EOF/heartbeat loss and restores hosts while terminating the final balena
   process group.

With the standard [Attraccess balena compose](https://github.com/Attraccess/Attraccess/blob/main/docker-compose.balena.yml),
you can open your configured `http://...` and `https://...` domains as usual,
reach Proxy Manager at `http://localhost:81`, and connect to direct bindings
using `localhost:<device-host-port>` (for example MQTT on `localhost:1883`).
HTTPS still uses the device's original certificate and the requested domain/SNI.

## Fullscreen terminal app

The app keeps the same header, setup steps and keyboard footer throughout the
session. Loading operations show animated spinners and remain cancellable.
Choose fleets with **Up/Down** or **j/k** and **Enter**; **/** searches fleet names
and slugs. Balena's actual device picker is embedded in the app using
[pyte's terminal emulator](https://pyte.readthedocs.io/en/latest/), preserving
its own selection and search behavior without letting its terminal controls
overwrite the app's frame.

If sudo needs authentication, enter your password in the masked field and press
**Enter**. **Backspace** edits and **Ctrl+U** clears the field; **Escape** or
**Ctrl+C** cancels. Your password is passed only to sudo's askpass helper. A
cached sudo authorization skips this prompt. Cleanup shows its own progress
screen before the original terminal is restored. Errors appear in the app and
stay visible until you dismiss them with **Enter**, **q**, or **Escape**.

The **Domains** view shows actual `http://` or `https://` links, the proxy ingress
port, and the destination service/container port. The selected row also shows
the full forwarding chain and configured upstream hostname. For example:

```text
https://app.example       → proxy:443 → attraccess:3000
https://grafana.example   → proxy:443 → grafana:3001
localhost:2883           → proxy:1883 → rabbitmq:1883
localhost:5380           → config-ui:5380
```

URLs use the proxy host's certificate configuration, independently of whether
its upstream uses HTTP or HTTPS. Remapped proxy ports appear in the URL, such
as `https://app.example:8443`. Custom location paths appear as separate routes;
redirection and 404 hosts are identified explicitly. Container identities are
resolved from live service names, network aliases and IP addresses. Unknown or
external upstreams retain their configured hostname instead of a guessed container.

- **Up/Down** or **j/k** selects a row; **Page Up/Page Down** scrolls a page.
- **Tab/Shift+Tab** switches between Domains, Ports and Notices; **1/2/3** jumps
  directly to a view. Each view remembers its selected row.
- **Enter** or **o** opens the selected domain URL in your browser. Browser
  launch runs independently so forwarding and cleanup remain responsive.
- URLs are [terminal hyperlinks](https://rich.readthedocs.io/en/stable/style.html#defining-styles)
  via Rich; click them using your terminal's usual link modifier. The visible
  text is also a normal URL for terminals that automatically recognize URLs.
- **q**, **Escape** or **Ctrl+C** stops forwarding and restores the hosts file
  and terminal. The dashboard adapts when the terminal is resized.

Use `--no-tui` for traditional terminal prompts and a static URL/mapping list.
Redirected output and `TERM=dumb` use this mode automatically. In an interactive
terminal, `--dry-run` follows the same fullscreen selection/discovery flow, then
keeps a **Preview** open until **q** or **Escape**. Preview URLs describe what will
be accessible once forwarding starts; dry runs do not activate local access.
Use `--dry-run --no-tui` for a discover-and-exit command.

## Options

```sh
# Choose the fleet, then choose a device using balena's native menu.
balena-proxy-tui

# Skip the fleet menu, then choose a device.
balena-proxy-tui myorg/myfleet

# Read the selected device's configuration without editing hosts or starting
# the persistent tunnel; no sudo required. Opens a fullscreen preview.
balena-proxy-tui myorg/myfleet --dry-run

# Print the discovery result and exit without the fullscreen app.
balena-proxy-tui myorg/myfleet --dry-run --no-tui

# Print URLs and container/port mappings without the fullscreen dashboard.
balena-proxy-tui myorg/myfleet --no-tui

# Choose a particular device (short or full UUID).
balena-proxy-tui 0123456789abcdef0123456789abcdef

# Supply a concrete name served by a wildcard proxy host; repeat as needed.
balena-proxy-tui myorg/myfleet --hostname app.example.org

# Override the NPM balena service name or CLI executable.
balena-proxy-tui myorg/myfleet --proxy-service my-proxy --balena /path/to/balena

# Adjust fleet loading, discovery and setup timeouts (menus have no timeout).
balena-proxy-tui myorg/myfleet --timeout 120

# Recover stale hosts records after a power loss or forced helper termination.
balena-proxy-tui --cleanup
```

For an uninstalled checkout, use `PYTHONPATH=src python3 -m attraccess_tunnel ...`.

## Hosts-file handling and limitations

Existing entries for a selected domain are temporarily removed from resolution;
other aliases on the same line stay active. The original lines are stored in
tagged, base64-encoded comments in `/etc/hosts` and restored on cleanup. Other
edits made during the session are retained, and the file's inode, ownership, and
permissions are preserved. One session at a time is allowed through an exclusive
lock. A subsequent start automatically recovers stale records before setting up
its own entries.

- Wildcards cannot be represented in `/etc/hosts`. The CLI reports them; use
  `--hostname` for each concrete name you need.
- Balena tunnels TCP. UDP bindings and streams are reported and skipped, so
  UDP-only services such as normal DNS queries are not forwarded.
- A local port already in use causes setup to fail and roll back. Stop that
  local service before retrying. All original port numbers are preserved so
  proxy URLs, redirects, and clients keep working with the standard compose.
- Disabled domain records are included but their routes remain disabled on the
  device. Host-network exposed ports are configuration hints; the associated
  service still needs to be listening. The CLI does not discover arbitrary
  unadvertised host-network sockets or forward private container ports.
- A device port bound exclusively to a particular remote LAN address may not be
  reachable through balena's tunnel; the CLI reports such bindings.
- If both the parent and privileged helper are forcibly killed, or the machine
  loses power, automatic cleanup cannot run. Use `--cleanup` or start another
  session. Do not remove the recovery comments before cleanup.
- The native picker integration reads balena's `Opening a tunnel to <UUID>...`
  status line. A CLI version that changes that message will need an adapter
  update; selection fails rather than guessing which device was chosen.

The tool reads the device's deployed configuration, so it does not need to clone
Attraccess or assume its current GitHub compose matches your deployed release.
See the official [balena SSH/tunnel command reference](https://docs.balena.io/reference/balena-cli/#device-tunnel).

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v

# Build the Nix package and run its checks.
nix build
nix flake check
```

Tests cover runtime domain/port discovery, both NPM database adapter styles,
interactive fleet/device selection, terminal restoration after selection/cancellation,
hosts restoration with concurrent edits, recovery, session locking, busy-port
rollback, PTY use, partial tunnel startup, TCP half-close behavior, and parent
EOF cleanup and remote command error propagation. Dashboard tests cover hyperlink
output, frontend TLS versus upstream schemes, container alias/IP resolution,
remapped ports, stream destinations, keyboard navigation, resizing, browser
launches, and terminal/hosts restoration on quit and signals. Fullscreen workflow
tests verify one alternate-screen session from startup through cleanup, native
picker terminal emulation, slow-operation cancellation, in-app errors, previews,
masked sudo askpass, session-scoped authorization and password-input cancellation.
They use temporary hosts files
and unprivileged ports; they never edit the machine's `/etc/hosts`. The remote
database script test additionally uses Node when available; the engine template
regression uses Go when available. A live balena device is needed to verify your fleet's
SSH permissions and end-to-end cloud connectivity.
