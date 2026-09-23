import urllib.request, json

mints = [
    ('Cateoween', '48n3WfUWrzQ6mNzwqNDFNVVvFLQBWn3bCHuTEsXPpump'),
    ('MEME1921', 'CEPZF289g6Mtyj4KdcC17Hci3Ld8eUyV3Fic6B3Apump'),
    ('WEBHUMAN', '5ytJHCHDsFQ7KBJywgLb54sTMmu5eaMpNbPoARHLpump'),
    ('Claude', 'GA2G6CnP7dxtRUFagkzEU6kzn8wrxdS5iMMjLZAnpump'),
    ('BASA', '6bejZgLMEV4dz8CMK6C21LYyqkALSs3wsQjjcZdKpump')
]

for sym, mint in mints:
    url = f'https://api.dexscreener.com/latest/dex/tokens/{mint}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            pairs = data.get('pairs') or []
            if pairs:
                p = pairs[0]
                mcap = p.get('marketCap') or p.get('fdv') or 0
                price_native = p.get('priceNative')
                price_usd = p.get('priceUsd')
                change_5m = p.get('priceChange', {}).get('m5')
                change_1h = p.get('priceChange', {}).get('h1')
                vol_5m = p.get('volume', {}).get('m5')
                print(f"[{sym}] MCap: ${mcap:,.0f} | Price Native: {price_native} SOL | Price USD: ${price_usd} | 5m: {change_5m}% | 1h: {change_1h}% | 5m Vol: ${vol_5m}")
            else:
                print(f"[{sym}] No pairs found on DexScreener yet")
    except Exception as e:
        print(f"[{sym}] Error: {e}")
