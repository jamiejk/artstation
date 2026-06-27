# Ink-dip hardware verification

Use scrap paper, water instead of ink, and keep a hand at the power switch.

1. Calibrate the plotter and save Home. Confirm ordinary Home return is within tolerance.
2. Place the empty well outside the artwork area. Move the head to its centre and select **Set Centre Here**.
3. Set a keep-out radius that includes the rim, mounting hardware, and positioning error.
4. Start with a conservative high clearance position and a shallow dip position. Save setup.
5. Fill the well with water and run **Test Cycle**. Confirm the tool clears the rim, does not bottom out, and returns to the exact starting point.
6. Adjust one calibration value at a time. Every adjustment invalidates the previous test.
7. After a successful test, mark the well installed.
8. Upload a two-stroke scrap SVG with automatic dipping enabled and a short interval. Check the reported dip count and longest-stroke warning before Start.
9. Run at low plot speed. Confirm the initial dip, checkpoint dip, resume position, final Home return, and job-history dip count.
10. Force one recoverable failure and confirm the job enters `dip_failed` without resuming automatically.

Do not use **Skip Dip & Resume** unless the tool is raised, the path to the saved checkpoint is clear, and the position reference is still valid. Cancel and recalibrate whenever position is uncertain.
