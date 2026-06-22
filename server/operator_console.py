import time
import urllib.request
import urllib.error
import json

SERVER = "http://127.0.0.1:8765"


def get_json(path: str):
    with urllib.request.urlopen(SERVER + path, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def post(path: str):
    request = urllib.request.Request(SERVER + path, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def bleep(times=3, gap=0.25):
    for _ in range(times):
        print("\a", end="", flush=True)
        time.sleep(gap)


def main():
    print("ArtStation operator console running.")
    print("Leave this terminal open on the Linux box.")
    print("Waiting for plotter prompts...\n")

    last_prompt_key = None

    while True:
        try:
            prompt = get_json("/operator/next")
        except Exception as exc:
            print(f"Server not available: {exc}")
            time.sleep(2)
            continue

        if prompt.get("active"):
            prompt_key = (prompt.get("job_id"), prompt.get("created_at"))

            if prompt_key != last_prompt_key:
                last_prompt_key = prompt_key

                print("\n" + "=" * 72)
                print(prompt.get("message"))
                print("=" * 72)
                bleep()

                input("Press ENTER when ready to continue... ")

                try:
                    result = post("/operator/continue")
                    print(result.get("message", "Continuing."))
                except Exception as exc:
                    print(f"Could not continue: {exc}")

        time.sleep(1)


if __name__ == "__main__":
    main()
