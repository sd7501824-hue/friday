import requests


def main():
    url = "http://127.0.0.1:8000/ask"
    payload = {"message": "Hello from client.py"}
    res = requests.post(url, json=payload, timeout=5)
    print(res.text)


if __name__ == "__main__":
    main()
