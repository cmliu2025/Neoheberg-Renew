#!/usr/bin/env python3
"""
节点分享链接 → 本地 SOCKS5 代理（基于 sing-box），并整理全局代理。

支持的节点协议: vless / vmess / trojan / ss / hysteria2(hy2) / tuic / socks5 / http

两种用法：
  ① 命令行（workflow 里的 "Setup proxy" 步骤）：准备"全局代理"
       NODE_LINK   全局节点链接（只取第一行）→ 本机 127.0.0.1:1080
       USER_PROXY  现成的全局代理地址（只取第一行）；NODE_LINK 不可用时作为后备
     成功后把全局代理写入 $GITHUB_ENV 的 PROXY_SERVER；
     配置了全局代理但都不可用时写入 GLOBAL_PROXY_FAILED=1（不中断 workflow，由 renew.py 决定哪些账号继续）。
  ② 被 renew.py 调用 start_nodes()：为各账号"专用代理"里的节点链接启动本地入口（端口从 2080 起）

每个节点单独做配置校验和连通性自检，不可用的节点返回 None。

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


def build_config(outbounds, base_port=PORT):
    """outbounds: 单个 outbound dict 或列表。第 i 个节点 → 入口 127.0.0.1:(base_port+i) → 出站 proxy{i}。"""
    if isinstance(outbounds, dict):
        outbounds = [outbounds]
    inbounds, obs, rules = [], [], []
    for i, ob in enumerate(outbounds):
        obs.append(dict(ob, tag=f"proxy{i}"))
        inbounds.append({"type": "socks", "tag": f"in{i}", "listen": "127.0.0.1", "listen_port": base_port + i})
        rules.append({"inbound": [f"in{i}"], "action": "route", "outbound": f"proxy{i}"})
    obs.append({"type": "direct", "tag": "direct"})
    return {
        "log": {"level": "warn"},
        "inbounds": inbounds,
        "outbounds": obs,
        "route": {"rules": rules, "final": "proxy0"},
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


def wait_port(port=PORT, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return True
        except OSError:
            time.sleep(0.5)
    return False


def split_lines(v):
    return [ln.strip() for ln in (v or "").replace("\r", "").split("\n")
            if ln.strip() and not ln.strip().startswith("#")]


def write_env(name, value):
    """写入 $GITHUB_ENV（多行值用 heredoc 语法）；本地运行则打印。"""
    env_file = os.environ.get("GITHUB_ENV")
    if not env_file:
        print(f"export {name}='{value}'")
        return
    delim = "EOF_" + base64.b16encode(os.urandom(6)).decode()
    with open(env_file, "a") as f:
        f.write(f"{name}<<{delim}\n{value}\n{delim}\n")


def mark_failed():
    """配置了代理但一个都不可用：留下标记，由 renew.py 取消相关账号（不会直连）。"""
    write_env("GLOBAL_PROXY_FAILED", "1")


def probe(port):
    """经本地入口访问外网，返回出口 IP；失败返回 None。"""
    r = subprocess.run(["curl", "-sS", "--max-time", "25", "--socks5-hostname",
                        f"127.0.0.1:{port}", "https://api.ipify.org"], capture_output=True, text=True)
    ip = r.stdout.strip()
    return ip if r.returncode == 0 and re.fullmatch(r"[0-9a-fA-F:.]{7,45}", ip) else None


def start_nodes(node_lines, base_port=PORT, tag="", label="节点"):
    """解析、校验并启动一批节点（单个 sing-box 进程，每个节点一个本地入口 base_port+j）。

    返回与 node_lines 等长的列表：可用节点为本地代理地址（socks5://127.0.0.1:端口），不可用为 None。
    任何环节失败都只影响对应节点，日志里不会出现节点的完整地址。
    """
    result = [None] * len(node_lines)
    parsed = []  # (序号, outbound)
    for i, link in enumerate(node_lines):
        try:
            ob = parse_link(link)
            parsed.append((i, ob))
            print(f"{label}#{i + 1}: {ob['type']} @ {mask(ob['server'], 3)}:{ob['server_port']}")
        except Exception as e:
            print(f"⚠️ {label}#{i + 1} 解析失败，已忽略: {type(e).__name__}: {e}")
    if not parsed:
        return result

    os.makedirs(WORKDIR, exist_ok=True)
    cfg_path = os.path.join(WORKDIR, f"config{tag}.json")
    if "--config-only" in sys.argv:
        with open(cfg_path, "w") as f:
            json.dump(build_config([ob for _, ob in parsed], base_port), f, indent=2)
        print("配置已生成:", cfg_path)
        return result

    try:
        binp = ensure_singbox()
    except Exception as e:
        print(f"⚠️ sing-box 下载失败: {type(e).__name__}: {str(e)[:100]}")
        return result

    # 每个节点先单独校验配置，坏节点不会拖累其他节点
    valid = []
    for i, ob in parsed:
        one = os.path.join(WORKDIR, f"check{tag}{i}.json")
        with open(one, "w") as f:
            json.dump(build_config([ob], base_port), f)
        chk = subprocess.run([binp, "check", "-c", one], capture_output=True, text=True)
        if chk.returncode == 0:
            valid.append((i, ob))
        else:
            print(f"⚠️ {label}#{i + 1} 配置校验失败，已忽略: {chk.stderr.strip()[:150]}")
    if not valid:
        return result

    with open(cfg_path, "w") as f:
        json.dump(build_config([ob for _, ob in valid], base_port), f, indent=2)
    logf = open(os.path.join(WORKDIR, f"sing-box{tag}.log"), "w")
    subprocess.Popen([binp, "run", "-c", cfg_path], stdout=logf, stderr=subprocess.STDOUT,
                     start_new_session=True)
    if not wait_port(base_port):
        print("⚠️ sing-box 未能启动")
        return result

    for j, (i, ob) in enumerate(valid):
        ip = probe(base_port + j)
        if ip:
            print(f"✅ {label}#{i + 1} 可用，出口 IP: {mask(ip, 3)}")
            result[i] = f"socks5://127.0.0.1:{base_port + j}"
        else:
            print(f"⚠️ {label}#{i + 1} 连通性检查失败，不可用")
    return result


def main():
    node_lines = split_lines(os.environ.get("NODE_LINK", ""))
    user_proxies = split_lines(os.environ.get("USER_PROXY", ""))
    if not node_lines and not user_proxies:
        print("未配置全局代理")
        return 0
    if len(node_lines) > 1:
        print(f"ℹ️ NODE_LINK 只取第一行作为全局代理（共 {len(node_lines)} 行）。账号需要各自的节点请写在 NEO_ACCOUNTS 的 proxy 字段里。")
    if len(user_proxies) > 1:
        print(f"ℹ️ PROXY_SERVER 只取第一行作为全局代理（共 {len(user_proxies)} 行）。")

    proxy = None
    if node_lines:
        proxy = start_nodes(node_lines[:1], label="全局节点")[0]
        if "--config-only" in sys.argv:
            return 0
        if not proxy and user_proxies:
            print("⚠️ 全局节点不可用，改用 PROXY_SERVER 作为全局代理")
    if not proxy and user_proxies:
        proxy = user_proxies[0]

    if not proxy:
        print("⚠️ 配置了全局代理但不可用。")
        print("   后续：配置了专用代理的账号继续登录；未配置专用代理的账号将被取消（不会直连）。")
        mark_failed()
        return 0  # 不让整个 workflow 在这里中断

    write_env("PROXY_SERVER", proxy)
    print("全局代理已就绪")
    return 0


if __name__ == "__main__":
    sys.exit(main())
