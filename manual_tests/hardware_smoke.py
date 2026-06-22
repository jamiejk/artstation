"""Manual hardware smoke test. Run explicitly; never import from automated tests."""

from pyaxidraw import axidraw


def main() -> None:
    ad = axidraw.AxiDraw()
    ad.interactive()
    ad.options.port = "/dev/ttyACM0"

    print("Connecting...")
    connected = ad.connect()
    print("Connected:", connected)

    if connected:
        ad.penup()
        ad.moveto(0, 0)
        ad.moveto(10, 0)
        ad.moveto(10, 10)
        ad.moveto(0, 10)
        ad.moveto(0, 0)
        ad.disconnect()


if __name__ == "__main__":
    main()
