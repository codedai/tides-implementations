# case C-ba6b0e92

- stratum: route_929 · kind: scheduled_trip
- summary: chunk of 6000s, anchor status ███ (hidden)
- vehicle: 9033 (apc-evidence)
- window: 2023-11-30 22:19:48 → 2023-11-30 23:59:48
- routes seen: 29

## candidates
- A: route 29 2023-11-30 22:14:00→2023-11-30 23:01:00 overlap 41m12s covered 86% tags=['starts terminal-near']
- B: route 29 2023-11-30 22:23:00→2023-11-30 23:10:00 overlap 47m00s covered 86% tags=['starts terminal-near']
- C: route 29 2023-11-30 23:02:00→2023-11-30 23:50:00 overlap 48m00s covered 87% tags=['direction reversed vs shape']
- D: route 29 2023-11-30 23:12:00→2023-12-01 00:00:00 overlap 47m48s covered 87% tags=['end boundary-near', 'direction reversed vs shape']
- E: route 29 2023-11-30 23:51:00→2023-12-01 00:38:00 overlap 8m48s covered 86% tags=['starts terminal-near']

## label
- ambiguous (confidence 4)
- notes: The traj is constructed with three trips belongs to candidate b, c, and e. With a shortturn between b and c at around 23:04:46,  stop until 23:13:52 and trip is following c then.  At around 23:51:00 the trip turns to candi e for another direction