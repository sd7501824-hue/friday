import requests, json

url = 'http://127.0.0.1:5050/ask'
for msg in ['hello', 'tell me a joke']:
    try:
        r = requests.post(url, json={'message': msg}, timeout=10)
        print('POST', msg, '->', r.status_code)
        try:
            print(json.dumps(r.json(), indent=2))
        except Exception:
            print(repr(r.text))
    except Exception as e:
        print('ERROR', e)
