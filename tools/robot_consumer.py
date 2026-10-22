"""Example event consumer - the robotics / teleop side of the wire.

    python tools/robot_consumer.py --listen 127.0.0.1:5005
    python -m snapdet live --source screen --show \
           --publish udp://127.0.0.1:5005 ...

Prints every select/hold/release event.  The two places a real system
hooks in are marked:

* ROBOT COMMAND: turn the assist point into your arm/gimbal/camera
  command (mind your own safety envelope and rate limits);
* CURSOR MAGNET: if this consumer IS your teleoperation UI and you
  want the pointer magnetised inside YOUR window, do it here -
  SetCursorPos on Windows, warp_pointer under X11 - your window, your
  rules.  snapdet itself never touches the OS cursor.
"""
import argparse
import json
import socket
import sys


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:5005")
    args = ap.parse_args(argv)
    host, port = args.listen.rsplit(":", 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((host, int(port)))
    s.settimeout(1.0)
    print("listening on %s:%s  (ctrl-c to stop)" % (host, port))
    last_kind = None
    while True:
        try:
            blob, _ = s.recvfrom(65535)
        except socket.timeout:
            continue
        except KeyboardInterrupt:
            break
        ev = json.loads(blob.decode("utf-8"))
        if ev["kind"] == "hold" and last_kind == "hold":
            line = ("\rhold  t=%.2f  assist=(%d,%d)  track #%d  %s/%s h%.0f"
                    % (ev["t"], ev["x"], ev["y"], ev["track_id"],
                       ev["view"], ev["stance"], ev["height"]))
            sys.stdout.write(line + " " * 20)
            sys.stdout.flush()
        else:
            sys.stdout.write("\n%s\n" % json.dumps(ev))
        last_kind = ev["kind"]
        if ev["kind"] == "select" and ev["track_id"] >= 0:
            # --- ROBOT COMMAND hook: e.g. point the camera gimbal at
            # (ev["x"], ev["y"]) in sender frame coordinates.
            pass
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
