#!/usr/bin/env python3
"""
Bridge do LEILÃO SURDO.
Serve o jogo + despacha rounds pro OpenCode rodando no GitHub Actions
(workflow leiloeiro-surdo) e devolve o JSON do robô quando o run termina.

Token: env GH_TOKEN ou arquivo .token ao lado (gitignorado).
"""
import json, os, re, sys, time, threading, urllib.request, urllib.error, urllib.parse, zipfile, io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OWNER = "Hhhaofuqoc"
REPO = "leilao-surdo"
WORKFLOW = "leiloeiro.yml"
API = "https://api.github.com"
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8788"))

def load_token():
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if t:
        return t.strip()
    p = os.path.join(HERE, ".token")
    if os.path.exists(p):
        return open(p).read().strip()
    return None

TOKEN = load_token()

def gh(path, data=None, method=None, raw=False):
    if not TOKEN:
        raise RuntimeError("sem token (GH_TOKEN ou .token)")
    url = API + path
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        method = method or "POST"
    req = urllib.request.Request(url, data=body, method=method or "GET")
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "leilao-surdo-bridge")
    with urllib.request.urlopen(req, timeout=30) as r:
        b = r.read()
        if raw:
            return b
        return json.loads(b) if b else None

class StripAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Nao manda Authorization pro host de destino (artifact URL assinada)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib.parse.urlparse(req.full_url).netloc
            new_host = urllib.parse.urlparse(newurl).netloc
            if old_host != new_host:
                new = urllib.request.Request(newurl, method=req.get_method())
                for k, v in req.headers.items():
                    if k.lower() not in ("authorization", "cookie"):
                        new.add_header(k, v)
        return new

OPENER = urllib.request.build_opener(StripAuthRedirect)

STATE = {}   # rid -> dict(payload, run_id, done, data, error, ts)
STATE_LOCK = threading.Lock()

def find_artifact(rid):
    """A API de workflow_dispatch NAO devolve inputs (redigido por seguranca).
    Correlaciono pelo nome do artifact que o workflow publica: resultado-<rid>.
    Retorna (art_id, run_id) ou (None, None) se ainda nao existe.
    O artifact so aparece quando o run termina com sucesso."""
    j = gh(f"/repos/{OWNER}/{REPO}/actions/artifacts?name=resultado-{rid}&per_page=1")
    arts = j.get("artifacts", [])
    if not arts:
        return None, None
    a = arts[0]
    if a.get("expired"):
        return None, None
    run_id = (a.get("workflow_run") or {}).get("id")
    return a["id"], run_id

def get_artifact_zip_by_id(art_id):
    url = f"{API}/repos/{OWNER}/{REPO}/actions/artifacts/{art_id}/zip"
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "leilao-surdo-bridge")
    with OPENER.open(req, timeout=30) as r:
        return r.read()

def parse_artifact(zbytes):
    with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
        name = z.namelist()[0]
        return json.loads(z.read(name).decode("utf-8"))

def poll(rid):
    """1 passo de polling. Retorna dict com status."""
    with STATE_LOCK:
        st = STATE.get(rid)
        if not st:
            return None
        if st["done"] or st.get("error"):
            return {"status": "done" if st["done"] else "error",
                    "data": st.get("data"), "error": st.get("error")}

    art_id, run_id = find_artifact(rid)

    if art_id:
        try:
            zb = get_artifact_zip_by_id(art_id)
            data = parse_artifact(zb)
        except Exception as e:
            with STATE_LOCK:
                st["error"] = f"artifact: {e}"
            return {"status": "error", "error": st["error"]}
        with STATE_LOCK:
            st["done"] = True
            st["data"] = data
            if run_id:
                st["run_id"] = run_id
        return {"status": "done", "data": data}

    # artifact ainda nao existe = run rodando (ou nao comecou)
    elapsed = time.time() - st["ts"]
    if elapsed > 300:
        with STATE_LOCK:
            st["error"] = "timeout (5min) - run nao publicou artifact"
        return {"status": "error", "error": st["error"]}

    # distingue "dispatch nao pegou" de "rodando": apos 45s sem artifact,
    # checa se existe algum run recente desse workflow pra dar feedback.
    if elapsed > 45 and not st.get("saw_run"):
        try:
            j = gh(f"/repos/{OWNER}/{REPO}/actions/workflows/{WORKFLOW}/runs?per_page=1")
            runs = j.get("workflow_runs", [])
            if runs:
                with STATE_LOCK:
                    st["saw_run"] = True
                    st["run_id"] = runs[0]["id"]
        except Exception:
            pass
    with STATE_LOCK:
        running = bool(st.get("run_id")) or st.get("saw_run")
    return {"status": "running" if (st.get("saw_run") or elapsed > 45) else "dispatched"}

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        sys.stderr.write("[bridge] " + (fmt % a) + "\n")

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def _file(self, path, ctype):
        with open(path, "rb") as f:
            b = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p in ("/", "/index.html"):
            return self._file(os.path.join(HERE, "index.html"), "text/html; charset=utf-8")
        m = re.match(r"^/api/leiloar/([a-f0-9]{16})$", p)
        if m:
            res = poll(m.group(1))
            if res is None:
                return self._json({"status": "unknown"}, 404)
            return self._json(res)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if p != "/api/leiloar":
            return self._json({"error": "not found"}, 404)
        try:
            ln = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(ln) or b"{}")
        except Exception:
            return self._json({"error": "json invalido"}, 400)

        rid = os.urandom(8).hex()
        payload = dict(payload)
        payload["rid"] = rid
        payload_str = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        try:
            gh(f"/repos/{OWNER}/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
               {"ref": "main", "inputs": {"rodada": payload_str}})
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            return self._json({"error": f"dispatch {e.code}: {detail}"}, 502)
        except Exception as e:
            return self._json({"error": str(e)}, 502)

        with STATE_LOCK:
            STATE[rid] = {"payload": payload_str, "run_id": None,
                          "done": False, "data": None, "error": None,
                          "ts": time.time()}
        return self._json({"rid": rid, "status": "dispatched"})

if __name__ == "__main__":
    if not TOKEN:
        print("ERRO: defina GH_TOKEN ou crie .token", file=sys.stderr)
        sys.exit(1)
    print(f"[bridge] LEILAO SURDO em http://127.0.0.1:{PORT}  repo={OWNER}/{REPO}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
