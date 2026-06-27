# Local AxiDraw servo range overrides for this UUNAtek/AxiDraw controller.
#
# These are the stock standard-servo defaults from axidraw_conf.py:
#   servo_min = 9855  -> 0.82 ms, pen position 0
#   servo_max = 27831 -> 2.32 ms, pen position 100
#
# The AxiDraw driver clamps pen_pos_up and pen_pos_down to 0-100.
#
# To get more physical downward travel at pen_pos_down=0, decrease servo_min
# in small steps and test Pen Down after each change.
#
# To get more physical lift at pen_pos_up=100, increase servo_max in small
# steps and test Pen Up after each change.
#
# Do not force the servo against a mechanical stop.
servo_min = 9000
servo_max = 28500
