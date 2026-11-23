#!/usr/bin/env python3
"""Exercise the real binary without bot tokens or Telegram network requests."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

binary = str(Path(sys.argv[1]).resolve())
env = {key: value for key, value in os.environ.items()
       if key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
       and not key.startswith("TELEGRAM_")}
# These deliberately fake application identifiers never authorize a bot.
env.update(TELEGRAM_API_ID="1", TELEGRAM_API_HASH="0" * 32)
cases = [
    (["--proxy-server=127.0.0.1"], {}, "proxy type must be specified"),
    (["--proxy-port=0"], {}, "proxy type must be specified"),
    (["--tdlib-proxy-type=socks5"], {}, "Proxy server must be specified"),
    (["--tdlib-proxy-type=http", "--proxy-server=127.0.0.1", "--proxy-port=65536"], {}, "Proxy port must be between"),
    (["--tdlib-proxy-type=mtproto", "--proxy-server=127.0.0.1", "--proxy-port=443"], {}, "Proxy secret must be specified"),
    (["--tdlib-proxy-type=bogus"], {}, "Unsupported TDLib proxy type"),
    ([], {"ALL_PROXY": "ftp://127.0.0.1:1080"}, "Invalid proxy URL"),
    (["--verbosity=-1"], {}, "Wrong verbosity level"),
]
for args, overrides, message in cases:
    result = subprocess.run([binary, *args], env={**env, **overrides},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0, (args, "accepted invalid configuration")
    assert message in result.stdout + result.stderr, (args, result.stdout, result.stderr)

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for extra, overrides in [
    (["--tdlib-proxy-type=http", "--proxy-server=127.0.0.1", "--proxy-port=8118"], {"ALL_PROXY": "invalid://ignored"}),
    ([], {"all_proxy": "socks5://127.0.0.1:1080", "ALL_PROXY": "invalid://lowercase-wins"}),
]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryFile() as log:
        process = subprocess.Popen([binary, "--http-ip-address=127.0.0.1",
                                    f"--http-port={port}", f"--dir={directory}",
                                    "--verbosity=0", *extra],
                                   env={**env, **overrides}, stdout=log, stderr=log)
        try:
            for _ in range(100):
                assert process.poll() is None, "server exited before startup"
                try:
                    opener.open(f"http://127.0.0.1:{port}/", timeout=0.2)
                except urllib.error.HTTPError as error:
                    assert error.code == 404, error.code
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.1)
                else:
                    raise AssertionError("root should return 404")
            else:
                raise AssertionError("loopback startup timed out")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
print(f"Passed {len(cases)} rejection cases and 2 isolated loopback startup cases")
