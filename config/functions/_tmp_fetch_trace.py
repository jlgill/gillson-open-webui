import base64
import json
import urllib.request

pub = "pk-lf-4ed34d90-956c-4a2e-84e4-70222ffb72e0"
sec = "sk-lf-1f668026-58dd-4e22-a6cf-e5cddc7d8728"
auth = base64.b64encode(f"{pub}:{sec}".encode()).decode()

TRACE_ID = "5f2d214fe5e2b1f7299b9f3ec90344b6"


def get(url):
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    resp = urllib.request.urlopen(req, timeout=15)
    return json.loads(resp.read().decode())


data = get(f"http://langfuse-web:3000/api/public/observations?traceId={TRACE_ID}&page=1&limit=100")
obs = data.get("data", [])
print("count:", len(obs))
with open("/tmp/lf_obs.json", "w") as f:
    json.dump(obs, f)

trace = get(f"http://langfuse-web:3000/api/public/traces/{TRACE_ID}")
with open("/tmp/lf_trace.json", "w") as f:
    json.dump(trace, f)
print("trace name:", trace.get("name"), "| session:", trace.get("sessionId"))
print("saved /tmp/lf_obs.json, /tmp/lf_trace.json")