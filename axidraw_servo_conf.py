# Local AxiDraw servo range overrides for this UUNAtek/AxiDraw controller.
#
# The AxiDraw driver clamps pen_pos_up and pen_pos_down to 0-100.
# Those map linearly onto [servo_min, servo_max] in EBB units (~83.3 ns).
#
# Probed 2026-07-22 (safe RC band with visible full travel):
#   servo_min ≈ deepest usable (software pen 0)
#   servo_max ≈ highest usable (software pen 100)
#
# To get more physical downward travel at pen_pos_down=0, decrease servo_min
# in small steps and test Pen Down after each change.
#
# To get more physical lift at pen_pos_up=100, increase servo_max in small
# steps and test Pen Up after each change.
#
# Do not force the servo against a mechanical stop.
servo_min = 7000   # ~0.58 ms — deeper than stock; leave margin above hard floor
servo_max = 30600  # ~2.55 ms — above prior 28500/29400; short of absolute probe max
