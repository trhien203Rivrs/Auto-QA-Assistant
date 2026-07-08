from preview import secs

assert secs("00:05") == 5
assert secs("01:30") == 90
assert secs("1:02:03") == 3723
print("ok")
