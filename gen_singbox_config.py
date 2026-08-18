#!/usr/bin/env python3
"""生成 sing-box 配置（从 TROJAN_URL 环境变量）。"""
import json, os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

parsed = urlsplit(os.environ["TROJAN_URL"])
query = parse_qs(parsed.query)
sni = query.get("sni", [parsed.hostname])[0]
ws_host = query.get("host", [sni])[0]
ws_path = unquote(query.get("path", ["/"])[0])
password = parsed.username or parsed.password

config = {
    "inbounds": [{"type": "mixed", "listen": "127.0.0.1", "listen_port": 1080}],
    "outbounds": [{
        "type": "trojan",
        "tag": "proxy",
        "server": parsed.hostname,
        "server_port": parsed.port,
        "password": password,
        "tls": {"enabled": True, "server_name": sni},
        "transport": {"type": "ws", "path": ws_path, "headers": {"Host": ws_host}},
    }],
    "route": {"final": "proxy"},
}
Path("/tmp/sing-box-config.json").write_text(json.dumps(config))
