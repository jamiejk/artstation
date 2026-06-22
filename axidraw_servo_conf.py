# Local AxiDraw servo range overrides for this UUNAtek/AxiDraw controller.
#
# These are the stock standard-servo defaults from axidraw_conf.py:
#   servo_min = 9855  -> 0.82 ms, pen position 0
#   servo_max = 27831 -> 2.32 ms, pen position 100
#
# If pen_pos_up=100 still does not lift far enough, increase servo_max in
# small steps, for example 28500, then test Pen Up. Do not force the servo
# against a mechanical stop.
servo_min = 9855
servo_max = 27831
