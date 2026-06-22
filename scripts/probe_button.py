#!/usr/bin/env python3
import time

import serial


PORT = "/dev/ttyACM0"


def main():
    with serial.Serial(PORT, timeout=1) as port:
        for index in range(10):
            port.write(b"QB\r")
            value = port.readline().decode("ascii", errors="replace").strip()
            ok = port.readline().decode("ascii", errors="replace").strip()
            print(f"{index}: value={value} ack={ok}", flush=True)
            time.sleep(0.2)


if __name__ == "__main__":
    main()
