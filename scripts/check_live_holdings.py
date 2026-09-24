import urllib.request
import json
import base64
import subprocess
import time

script = '''
import urllib.request, json

mints = {
    "CoJN4WCUYtYPzU9ZcjfRyL69Txocf6YUUbdAaHvJpump": ("SOLDNA", 607973.966448),
    "YzUi8RJeMkHzeRMGaCwrfdKcv71bdAMGK3EWM1dpump": ("UDR", 148.067462),
    "2xxefXTw9Q5QhD1LVgC9ipibgA57LHA4XWM7iwpb63cH": ("ROMO", 133340.171278),
    "A7oP5SRMxqi1DY6Htr2GBscgmiBhZurK44gDDiBZx8Ar": ("IshiGo", 423859.267285),
    "GF4bPScrNWKd4xX728we4iBbFrtbbEBGDZhgztaZpump": ("CONDOM", 307247.453551),
    "GNhCphYjduivkJvzSqWiwTyjvJsZmzUtrSKVjrhFpump": ("DUST", 3120.0),
    "GXLTeBynceAsNDCkKCVW18cJMT92PUVk7jprdGQmpump": ("TS", 274754.59345)
}

print(f"{'SYMBOL':<10} {'BALANCE':<15} {'PRICE_SOL':<14} {'VALUE_SOL':<12} {'5m_VOL':<10} {'LIQ_USD':<12}")
print("-" * 75)
tot_val = 0.0
for mint, (sym, bal) in mints.items():
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        req = urllib.request.Request(url, headers={"User-Agent": "Botsensai"})
        data = json.loads(urllib.request.urlopen(req, timeout=3).read().decode())
        pairs = data.get("pairs") or []
        if pairs:
            best = max(pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
            px_sol = float(best.get("priceNative") or 0.0)
            vol5m = float(best.get("volume", {}).get("m5") or 0.0)
            liq = float(best.get("liquidity", {}).get("usd") or 0.0)
            dex_sym = best.get("baseToken", {}).get("symbol") or sym
        else:
            px_sol, vol5m, liq, dex_sym = 0.0, 0.0, 0.0, sym
    except Exception as e:
        px_sol, vol5m, liq, dex_sym = 0.0, 0.0, 0.0, sym

    val_sol = px_sol * bal
    tot_val += val_sol
    print(f"{dex_sym:<10} {bal:<15,.1f} {px_sol:<14.8f} {val_sol:<12.4f} ${vol5m:<9.0f} ${liq:<11.0f}")

print("-" * 75)
print(f"TOTAL TOKEN VALUE: {tot_val:.4f} SOL")
'''

b64 = base64.b64encode(script.encode()).decode()

res = subprocess.run([
    "aws", "ssm", "send-command",
    "--profile", "agent-profile",
    "--region", "us-east-1",
    "--instance-ids", "i-0bb5f0e7d264a2937",
    "--document-name", "AWS-RunShellScript",
    "--parameters", f"commands=[\"echo {b64} | base64 -d | /home/ubuntu/Botsensai/.venv/bin/python\"]",
    "--output", "json"
], capture_output=True, text=True)

cmd = json.loads(res.stdout)["Command"]["CommandId"]
time.sleep(3)
res2 = subprocess.run([
    "aws", "ssm", "get-command-invocation",
    "--profile", "agent-profile",
    "--region", "us-east-1",
    "--command-id", cmd,
    "--instance-id", "i-0bb5f0e7d264a2937",
    "--output", "json"
], capture_output=True, text=True)

out = json.loads(res2.stdout)
print("STDOUT:\n" + out.get("StandardOutputContent", ""))
if out.get("StandardErrorContent"):
    print("STDERR:\n" + out.get("StandardErrorContent", ""))
