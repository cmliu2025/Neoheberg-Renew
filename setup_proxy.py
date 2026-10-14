#!/usr/bin/env python3
"""
把节点分享链接（NODE_LINK）转成 sing-box 配置，并在本机 127.0.0.1:1080 启动 SOCKS5 代理。

支持: vless / vmess / trojan / ss / hysteria2(hy2) / tuic / socks5 / http
成功后把 PROXY_SERVER=socks5://127.0.0.1:1080 写入 $GITHUB_ENV，供 renew.py 使用。

用法:
  NODE_LINK='vless://...' python setup_proxy.py
  NODE_LINK='...' python setup_proxy.py --config-only   # 只生成配置，不下载/启动（调试用）
"""
import base64
import io
import json
import os
import platform
import re
import socket
import subprocess
import sys
import tarfile
import time
import urllib.parse as up
import urllib.request as ur

PORT = 1080
WORKDIR = os.environ.get("SB_DIR", "/tmp/sing-box")


# ───────────────────────── 工具 ─────────────────────────
def mask(s, show=2):
    s = str(s)
    return s if len(s) <= show * 2 else s[:show] + "*" * (len(s) - show * 2) + s[-show:]


def b64d(s):
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s).decode("utf-8", "ignore")


def truthy(v):
    return str(v or "").lower() in ("1", "true", "yes")


def qs(u):
    return {k: v[0] for k, v in up.parse_qs(u.query).items()}


def tls_block(q, default_sni, reality=False):
    tls = {"enabled": True, "server_name": q.get("sni") or q.get("peer") or default_sni}
    if truthy(q.get("allowInsecure")) or truthy(q.get("insecure")) or truthy(q.get("allow_insecure")):
        tls["insecure"] = True
    if q.get("alpn"):
        tls["alpn"] = [a for a in q["alpn"].split(",") if a]
    fp = q.get("fp")
    if reality:
        tls["utls"] = {"enabled": True, "fingerprint": fp or "chrome"}
        tls["reality"] = {"enabled": True, "public_key": q.get("pbk", ""), "short_id": q.get("sid", "")}
    elif fp:
        tls["utls"] = {"enabled": True, "fingerprint": fp}
    return tls


def transport(q):
    t = (q.get("type") or "tcp").lower()
    path = q.get("path") or "/"
    host = q.get("host")
    if t == "ws":
        tr = {"type": "ws"}
        m = re.search(r"[?&]ed=(\d+)", path)
        if m:  # 常见的 CF Workers early data 写法
            tr["max_early_data"] = int(m.group(1))
            tr["early_data_header_name"] = "Sec-WebSocket-Protocol"
            path = re.sub(r"[?&]ed=\d+", "", path) or "/"
        tr["path"] = path
        if host:
            tr["headers"] = {"Host": host}
        return tr
    if t == "grpc":
        return {"type": "grpc", "service_name": q.get("serviceName") or q.get("path") or ""}
    if t in ("h2", "http"):
        tr = {"type": "http", "path": path}
        if host:
            tr["host"] = host.split(",")
        return tr
    if t == "httpupgrade":
        tr = {"type": "httpupgrade", "path": path}
        if host:
            tr["host"] = host
        return tr
    return None


def base_out(kind, host, port):
    return {"type": kind, "tag": "proxy", "server": host, "server_port": int(port)}


# ───────────────────────── 各协议解析 ─────────────────────────
def parse_vless(link):
    u = up.urlparse(link)
    q = qs(u)
    out = base_out("vless", u.hostname, u.port)
    out["uuid"] = up.unquote(u.username or "")
    if q.get("flow"):
        out["flow"] = q["flow"]
    sec = (q.get("security") or "none").lower()
    if sec in ("tls", "reality"):
        out["tls"] = tls_block(q, u.hostname, reality=(sec == "reality"))
    tr = transport(q)
    if tr:
        out["transport"] = tr
    return out


def parse_vmess(link):
    d = json.loads(b64d(link[len("vmess://"):]))
    out = base_out("vmess", d["add"], d["port"])
    out["uuid"] = d["id"]
    out["security"] = d.get("scy") or "auto"
    out["alter_id"] = int(d.get("aid") or 0)
    q = {"type": d.get("net", "tcp"), "path": d.get("path"), "host": d.get("host"),
         "sni": d.get("sni"), "alpn": d.get("alpn"), "fp": d.get("fp")}
    if str(d.get("tls", "")).lower() in ("tls", "1", "true"):
        out["tls"] = tls_block(q, d.get("host") or d["add"])
    tr = transport(q)
    if tr:
        out["transport"] = tr
    return out


def parse_trojan(link):
    u = up.urlparse(link)
    q = qs(u)
    out = base_out("trojan", u.hostname, u.port)
    out["password"] = up.unquote(u.username or "")
    if (q.get("security") or "tls").lower() != "none":
        out["tls"] = tls_block(q, u.hostname)
    tr = transport(q)
    if tr:
        out["transport"] = tr
    return out


def parse_ss(link):
    rest = link[len("ss://"):].split("#", 1)[0]
    query = ""
    if "?" in rest:
        rest, query = rest.split("?", 1)
    rest = rest.rstrip("/")
    if "@" in rest:
        userinfo, hostport = rest.rsplit("@", 1)
        userinfo = up.unquote(userinfo) if ":" in up.unquote(userinfo) else b64d(up.unquote(userinfo))
    else:
        userinfo, hostport = b64d(rest).rsplit("@", 1)
    if "plugin" in query:
        raise ValueError("暂不支持带 plugin 的 ss 链接")
    method, password = userinfo.split(":", 1)
    host, port = hostport.rsplit(":", 1)
    out = base_out("shadowsocks", host.strip("[]"), port)
    out["method"] = method
    out["password"] = password
    return out


def parse_hy2(link):
    u = up.urlparse(link)
    q = qs(u)
    out = base_out("hysteria2", u.hostname, u.port or 443)
    pw = up.unquote(u.username or "")
    if u.password:
        pw += ":" + up.unquote(u.password)
    out["password"] = pw
    if q.get("obfs"):
        out["obfs"] = {"type": q["obfs"], "password": q.get("obfs-password", "")}
    out["tls"] = tls_block(q, u.hostname)
    return out


def parse_tuic(link):
    u = up.urlparse(link)
    q = qs(u)
    out = base_out("tuic", u.hostname, u.port)
    out["uuid"] = up.unquote(u.username or "")
    out["password"] = up.unquote(u.password or "")
    out["congestion_control"] = q.get("congestion_control", "bbr")
    out["udp_relay_mode"] = q.get("udp_relay_mode", "native")
    out["tls"] = tls_block(q, u.hostname)
    return out


def parse_plain(link):
    u = up.urlparse(link)
    scheme = u.scheme.lower()
    if scheme.startswith("socks"):
        out = base_out("socks", u.hostname, u.port or 1080)
        out["version"] = "5"
    else:
        out = base_out("http", u.hostname, u.port or (443 if scheme == "https" else 80))
        if scheme == "https":
            out["tls"] = {"enabled": True, "server_name": u.hostname}
    if u.username:
        out["username"] = up.unquote(u.username)
        out["password"] = up.unquote(u.password or "")
    return out


PARSERS = {
    "vless": parse_vless, "vmess": parse_vmess, "trojan": parse_trojan, "ss": parse_ss,
    "hysteria2": parse_hy2, "hy2": parse_hy2, "tuic": parse_tuic,
    "socks": parse_plain, "socks5": parse_plain, "socks5h": parse_plain,
    "http": parse_plain, "https": parse_plain,
}


def parse_link(link):
    scheme = link.strip().split("://", 1)[0].lower()
    if scheme not in PARSERS:
        raise ValueError(f"不支持的协议: {scheme}")
    return PARSERS[scheme](link.strip())


def build_config(outbound):
    return {
        "log": {"level": "warn"},
        "inbounds": [{"type": "socks", "tag": "in", "listen": "127.0.0.1", "listen_port": PORT}],
        "outbounds": [outbound, {"type": "direct", "tag": "direct"}],
        "route": {"final": "proxy"},
    }


# ───────────────────────── sing-box 下载 / 启动 ─────────────────────────
def ensure_singbox():
    """不走 GitHub API（匿名有限流）：跟随 /releases/latest 的重定向拿到版本号，再按文件名直接下载。"""
    binp = os.path.join(WORKDIR, "sing-box")
    if os.path.exists(binp):
        return binp
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower(), "amd64")
    ver = os.environ.get("SB_VERSION", "").lstrip("v")
    if not ver:
        final = ur.urlopen(ur.Request("https://github.com/SagerNet/sing-box/releases/latest",
                                      headers={"User-Agent": "setup-proxy"}), timeout=30).geturl()
        ver = final.rsplit("/", 1)[-1].lstrip("v")
    data = None
    for suffix in ("", "-glibc", "-musl"):  # 不同版本的发行包命名略有差异，依次尝试
        url = (f"https://github.com/SagerNet/sing-box/releases/download/v{ver}/"
               f"sing-box-{ver}-linux-{arch}{suffix}.tar.gz")
        try:
            print("下载 sing-box:", url.rsplit("/", 1)[-1])
            data = ur.urlopen(ur.Request(url, headers={"User-Agent": "setup-proxy"}), timeout=120).read()
            break
        except Exception as e:
            print("  失败:", type(e).__name__, str(e)[:80])
    if data is None:
        raise RuntimeError(f"无法下载 sing-box v{ver}")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        member = next(m for m in tf.getmembers() if m.isfile() and os.path.basename(m.name) == "sing-box")
        with open(binp, "wb") as f:
            f.write(tf.extractfile(member).read())
    os.chmod(binp, 0o755)
    return binp


def wait_port(timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=1).close()
            return True
        except OSError:
            time.sleep(0.5)
    return False


def main():
    link = os.environ.get("NODE_LINK", "").strip()
    if not link:
        print("未设置 NODE_LINK")
        return 1
    try:
        outbound = parse_link(link)
    except Exception as e:
        print(f"❌ 节点链接解析失败: {type(e).__name__}: {e}")
        return 1
    print(f"节点: {outbound['type']} @ {mask(outbound['server'], 3)}:{outbound['server_port']}")

    os.makedirs(WORKDIR, exist_ok=True)
    cfg_path = os.path.join(WORKDIR, "config.json")
    with open(cfg_path, "w") as f:
        json.dump(build_config(outbound), f, indent=2)
    if "--config-only" in sys.argv:
        print("配置已生成:", cfg_path)
        return 0

    binp = ensure_singbox()
    chk = subprocess.run([binp, "check", "-c", cfg_path], capture_output=True, text=True)
    if chk.returncode != 0:
        print("❌ sing-box 配置校验失败:", chk.stderr.strip()[:300])
        return 1

    logf = open(os.path.join(WORKDIR, "sing-box.log"), "w")
    subprocess.Popen([binp, "run", "-c", cfg_path], stdout=logf, stderr=subprocess.STDOUT,
                     start_new_session=True)
    if not wait_port():
        print("❌ sing-box 未能启动")
        print(open(os.path.join(WORKDIR, "sing-box.log")).read()[-500:])
        return 1

    # 连通性自检：经代理访问外网，确认节点可用
    r = subprocess.run(["curl", "-sS", "--max-time", "25", "--socks5-hostname",
                        f"127.0.0.1:{PORT}", "https://api.ipify.org"], capture_output=True, text=True)
    ip = r.stdout.strip()
    if r.returncode != 0 or not ip:
        print("❌ 代理连通性检查失败（节点不可用？）:", r.stderr.strip()[:200])
        return 1
    print(f"✅ 代理可用，出口 IP: {mask(ip, 3)}")

    proxy = f"socks5://127.0.0.1:{PORT}"
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a") as f:
            f.write(f"PROXY_SERVER={proxy}\n")
    else:
        print(f"export PROXY_SERVER={proxy}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
