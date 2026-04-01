# FTMS Integration Fix Plan — Session Resilience

## Problem Statement

A BLE disconnect during an active workout (SOLE_ACTIVE state) triggers a full
integration reload instead of a graceful reconnect. This causes:
1. All entities go `unavailable → off → on`
2. The START automation re-fires, destroying accumulated local samples
3. 27 minutes of workout data lost in the 2026-04-01 incident

Additionally, after the workout ends (speed=0), `workout_active` stays `on` for
~30 minutes because the integration never sends the False event when using the
idle timeout path.

## Root Cause

`_on_disconnect()` (line 344) only suppresses reload when `_hybrid_st == RECONNECTING`.
An unexpected BLE drop during `SOLE_ACTIVE` triggers a reload. The reload re-initializes
the integration, treadmill is still running, so `_activate()` fires again, sending a
duplicate `WORKOUT_ACTIVE_KEY: True`. The START automation clears the local samples file.

---

## Fix 1: Handle unexpected BLE disconnect with hybrid reconnect

### What
In `_on_disconnect()`, when state is `SOLE_ACTIVE` or `ACTIVATING`, trigger
`_hybrid_reconnect(is_pause=True)` instead of scheduling a reload.

### Where
`__init__.py`, lines 344-349 (`_on_disconnect` function)

### Current code
```python
def _on_disconnect(ftms_: pyftms.FitnessMachine) -> None:
    if _hybrid_st == _HybridState.RECONNECTING:
        return
    if ftms_.need_connect:
        hass.config_entries.async_schedule_reload(entry.entry_id)
```

### New code
```python
def _on_disconnect(ftms_: pyftms.FitnessMachine) -> None:
    _LOGGER.warning(
        "BLE disconnected (state=%s, need_connect=%s)",
        _hybrid_st.value if _hybrid_st else None,
        ftms_.need_connect,
    )
    if _hybrid_st == _HybridState.RECONNECTING:
        return  # Already handling reconnect
    if _hybrid_st in (_HybridState.SOLE_ACTIVE, _HybridState.ACTIVATING):
        # Mid-workout BLE drop — reconnect gracefully, keep session alive
        _track_task(hass.async_create_task(_hybrid_reconnect(is_pause=True)))
        return
    if ftms_.need_connect:
        hass.config_entries.async_schedule_reload(entry.entry_id)
```

### Effect
BLE drops during a workout trigger a 3-retry reconnect instead of a full reload.
If all retries fail, the existing code in `_hybrid_reconnect` schedules a reload
as last resort (line 574-577). Session stays alive through transient drops.

---

## Fix 2: Prevent duplicate WORKOUT_ACTIVE_KEY: True events

### What
Add a `_workout_active` flag that tracks whether the integration has already
signaled the session as active. `_activate()` only sends the True event if the
flag is False. This prevents the START automation from re-triggering on BLE
reconnect mid-workout.

### Where
`__init__.py`:
- Declaration: near line 435 (with other state variables)
- Set True: in `_activate()`, line 526-528
- Set False: in `_hybrid_reconnect()`, line 580-583 (conditional)
- Reset: in `async_unload_entry()` (cleanup)

### Changes

#### a) Declare the flag (near line 435)
```python
_workout_active = False  # True once WORKOUT_ACTIVE_KEY: True sent
```

#### b) Guard the True event in `_activate()` (lines 526-528)
```python
# _activate() already has: nonlocal _hybrid_st, _speed_positive_count
# Change to: nonlocal _hybrid_st, _speed_positive_count, _workout_active

# Current:
coordinator.async_set_updated_data(
    UpdateEvent(event_id="update", event_data={WORKOUT_ACTIVE_KEY: True})
)

# New:
if not _workout_active:
    _workout_active = True
    coordinator.async_set_updated_data(
        UpdateEvent(event_id="update", event_data={WORKOUT_ACTIVE_KEY: True})
    )
```

#### c) Set False in `_hybrid_reconnect()` (lines 580-583)
```python
# _hybrid_reconnect() already has: nonlocal _hybrid_st, _speed_positive_count
# Change to: nonlocal _hybrid_st, _speed_positive_count, _workout_active

# Current:
if not is_pause:
    coordinator.async_set_updated_data(
        UpdateEvent(event_id="update", event_data={WORKOUT_ACTIVE_KEY: False})
    )

# New (merged with Fix 3 Change A and Fix 4):
# See Fix 3 Change A for the full replacement of lines 579-583.
```

Note: The actual code for lines 579-583 is shown in Fix 3 Change A, which
combines Fix 2's `_workout_active = False`, Fix 3's `_start_idle_timer()`,
and Fix 4's `_end_workout_pending` check into a single coherent block.

### Effect
On BLE reconnect mid-workout: `_activate()` fires again (speed > 0 threshold met),
but `_workout_active` is already True, so no True event is sent. The START automation
does not re-fire. Local samples are preserved.

---

## Fix 3: End session after idle timeout if speed is still 0

### Background
The Sole F63 sometimes doesn't send EndWorkout (0x32) when the user stops the
treadmill. This is a known device quirk — not related to BLE drops. The idle
timeout (speed=0 for 60s) is the safety net. Currently it calls
`_hybrid_reconnect(is_pause=True)` which reconnects BLE (to unblock the physical
START button) but never sends `WORKOUT_ACTIVE_KEY: False`. The session stays
alive until BLE idle disconnect ~30 minutes later.

### What
Use a two-stage idle timeout. The state machine itself distinguishes the stages:
- **First timeout (SOLE_ACTIVE):** reconnect with `is_pause=True` — BLE is
  unblocked, session stays alive in case the user resumes.
- **Second timeout (FTMS_IDLE + `_workout_active`):** speed is still 0 after
  reconnect, so the workout is truly over — send `WORKOUT_ACTIVE_KEY: False`.

No counter variable needed — the hybrid state already tells us which stage we're in.

### Why not a counter?
A counter approach (`_idle_timeout_count`) was considered and rejected during audit.
After the first idle timeout, `_hybrid_reconnect(is_pause=True)` transitions state
to `FTMS_IDLE`. But the idle timer is only started from code paths gated on
`SOLE_ACTIVE` (in `_on_sole_event` line 482, `_on_ftms_raw_notify` line 630, and
`_activate` line 525). In `FTMS_IDLE`, none of these paths fire — the timer never
restarts, the counter never reaches 2. The session never ends.

### Where (3 changes)

#### Change A: Re-arm idle timer after pause reconnect
`__init__.py`, in `_hybrid_reconnect()`, after line 579.

```python
_hybrid_st = _HybridState.FTMS_IDLE

# Check if EndWorkout arrived during reconnect (Fix 4)
if _end_workout_pending:
    _end_workout_pending = False
    is_pause = False  # Override — EndWorkout means session is over

if not is_pause:
    _workout_active = False
    coordinator.async_set_updated_data(
        UpdateEvent(event_id="update", event_data={WORKOUT_ACTIVE_KEY: False})
    )
elif _workout_active:
    # Session still alive after pause-reconnect. Re-arm idle timer so that
    # if speed stays 0 for another 60s, we end the session (stage 2).
    _start_idle_timer()
```

#### Change B: Let `_idle_timeout` handle FTMS_IDLE + active session
`__init__.py`, `_idle_timeout` function (lines 452-461).

```python
@callback
def _idle_timeout(_now):
    nonlocal _idle_timer_unsub
    _idle_timer_unsub = None
    if _hybrid_st == _HybridState.SOLE_ACTIVE:
        # Stage 1: reconnect to unblock START, keep session alive
        _LOGGER.warning(
            "Speed-zero timeout (%ds) — reconnecting to unblock START",
            _SOLE_IDLE_TIMEOUT,
        )
        _track_task(hass.async_create_task(
            _hybrid_reconnect(is_pause=True)
        ))
    elif _hybrid_st == _HybridState.FTMS_IDLE and _workout_active:
        # Stage 2: still speed=0 after reconnect — workout is over
        _LOGGER.warning(
            "Post-reconnect idle timeout (%ds) — ending workout session",
            _SOLE_IDLE_TIMEOUT,
        )
        _workout_active = False
        coordinator.async_set_updated_data(
            UpdateEvent(
                event_id="update",
                event_data={WORKOUT_ACTIVE_KEY: False},
            )
        )
```

Must use `nonlocal _workout_active` (add to existing nonlocal declarations
in the enclosing scope, or add a new one if `_idle_timeout` doesn't have one).

Note: Stage 2 sends the False event directly — no BLE reconnect needed because
we're already in FTMS_IDLE with a healthy connection.

#### Change C: Handle speed changes in FTMS_IDLE when session is alive
`__init__.py`, `_on_ftms_raw_notify` function, FTMS_IDLE branch (lines 618-629).

```python
if _hybrid_st == _HybridState.FTMS_IDLE:
    if speed > 0:
        _speed_positive_count += 1
        if _workout_active:
            _cancel_idle_timer()  # User resumed — cancel session-end timer
        if _speed_positive_count >= _SOLE_ACTIVATION_THRESHOLD:
            _hybrid_st = _HybridState.ACTIVATING
            _LOGGER.warning(
                "FTMS speed=%.2f > 0 for %d consecutive notifications, activating Sole",
                speed, _speed_positive_count,
            )
            _track_task(hass.async_create_task(_activate()))
    else:
        _speed_positive_count = 0
        if _workout_active:
            _start_idle_timer()  # Re-arm if session alive and speed=0
```

This handles the edge case where speed briefly blips above 0 (belt coasting)
then returns to 0 after the pause-reconnect.

### Effect — normal workout end without EndWorkout

```
T+0:00   Speed=0, state=SOLE_ACTIVE → idle timer starts (existing code)
T+1:00   Stage 1: _idle_timeout fires in SOLE_ACTIVE
         → _hybrid_reconnect(is_pause=True)
         → reconnect → state=FTMS_IDLE
         → is_pause=True, _workout_active=True → _start_idle_timer()  [Change A]
T+1:05   FTMS data: speed=0, FTMS_IDLE, _workout_active=True
         → _start_idle_timer() no-op (already running)              [Change C]
T+2:00   Stage 2: _idle_timeout fires in FTMS_IDLE + _workout_active
         → WORKOUT_ACTIVE_KEY: False sent directly                  [Change B]
         → Session ends cleanly. Total delay: ~120s from speed=0.
```

### Effect — pause and resume

```
T+0:00   Speed=0, state=SOLE_ACTIVE → idle timer starts
T+1:00   Stage 1: reconnect with is_pause=True → FTMS_IDLE
         → idle timer re-armed                                      [Change A]
T+1:20   User presses START, speed=3.0
         → _cancel_idle_timer()                                     [Change C]
         → activation threshold met → SOLE_ACTIVE
         → _workout_active already True → no duplicate True event   [Fix 2]
         → Session continues normally.
```

### Effect — BLE drop during workout, treadmill already stopped

```
T+0:00   BLE drops while state=SOLE_ACTIVE (treadmill stopped moments ago)
         → Fix 1: _hybrid_reconnect(is_pause=True)
         → reconnect → FTMS_IDLE → idle timer re-armed              [Change A]
T+0:05   FTMS data: speed=0
         → _start_idle_timer() no-op                                [Change C]
T+1:00   Stage 2: _idle_timeout fires in FTMS_IDLE + _workout_active
         → WORKOUT_ACTIVE_KEY: False → session ends                 [Change B]
```

---

## Fix 4: Handle EndWorkout during RECONNECTING state

### What
If EndWorkout (0x32) arrives while `_hybrid_st == RECONNECTING` (e.g. idle timeout
reconnect is in progress), set a flag so the False event is sent after reconnect
completes.

### Where
`__init__.py`:
- Flag declaration: near line 435
- Set flag: in `_on_end_workout()` (lines 585-592)
- Check flag: in `_hybrid_reconnect()` after success path

### Changes

#### a) Declare flag (near line 435)
```python
_end_workout_pending = False
```

#### b) Set flag in `_on_end_workout()` (lines 585-592)
```python
def _on_end_workout():
    nonlocal _end_workout_pending
    _cancel_idle_timer()
    if _hybrid_st == _HybridState.RECONNECTING:
        _LOGGER.warning("EndWorkout received during reconnect — will end session after")
        _end_workout_pending = True
        return
    if _hybrid_st not in (_HybridState.SOLE_ACTIVE, _HybridState.ACTIVATING):
        _LOGGER.warning("EndWorkout received but state=%s, ignoring",
                        _hybrid_st.value if _hybrid_st else None)
        return
    _LOGGER.warning("EndWorkout received in state=%s, triggering reconnect",
                    _hybrid_st.value)
    _track_task(hass.async_create_task(_hybrid_reconnect()))
```

#### c) Check flag after reconnect success (after line 579)

Already shown in Fix 3 Change A — the full replacement of lines 579-583 includes
the `_end_workout_pending` check. `_hybrid_reconnect` needs:
```python
nonlocal _hybrid_st, _speed_positive_count, _workout_active, _end_workout_pending
```

### Effect
EndWorkout arriving during an idle-timeout reconnect is no longer lost. The session
ends cleanly after reconnect completes.

---

## Fix 5: Add logging to `_on_disconnect`

Already included in Fix 1. The `_LOGGER.warning()` call logs `_hybrid_st` and
`need_connect` on every BLE disconnect.

---

## Implementation Order

1. **Fix 5** (logging) — zero risk, immediate diagnostic value
2. **Fix 2** (`_workout_active` flag) — prevents data loss, low risk
3. **Fix 1** (reconnect instead of reload) — the main fix, moderate risk
4. **Fix 3** (two-stage idle timeout) — session end detection, moderate risk
5. **Fix 4** (EndWorkout during RECONNECTING) — edge case, low risk

Fixes 1+2 together solve the data loss bug from the 2026-04-01 incident.
Fix 3 solves the "workout_active stuck on for 30 min" issue.
Fix 4 is a correctness improvement for a low-probability race condition.

Note: Fixes 2, 3, and 4 all modify the end of `_hybrid_reconnect()` (lines 579-583).
The combined replacement is shown in Fix 3 Change A.

## Known Limitations

**Reconnect failure fallback:** If Fix 1 triggers `_hybrid_reconnect(is_pause=True)`
and all 3 retries fail, the fallback is `async_schedule_reload`. A reload re-runs
`async_setup_entry`, resetting `_workout_active = False` (closure variable). If the
treadmill becomes reachable again, `_activate()` sends `WORKOUT_ACTIVE_KEY: True`
and the START automation re-fires — the same data loss as before.

This is acceptable: 3 consecutive BLE failures means the connection is truly down,
not a transient drop. A reload is the correct recovery. The data loss risk only
exists if the treadmill somehow becomes reachable again after 3 failures, which
is a much rarer scenario than the single-drop case that Fix 1+2 handles.

---

## Testing Plan

### Test 1: Normal workout (baseline)
- Start treadmill, exercise, stop, wait for EndWorkout
- Verify: one clean session, all samples present, workout_active goes off

### Test 2: BLE disconnect mid-workout
- Start treadmill, exercise at high speed
- Kill the gym BLE proxy (power cycle m5stack-atom-lite-f99ef4)
- Restore proxy within 30s
- Verify: session continues, no sample loss, no duplicate START trigger

### Test 3: BLE disconnect mid-workout (proxy stays down)
- Same as Test 2 but leave proxy down for 2+ minutes
- Verify: all 3 reconnect retries fail, integration reloads cleanly
- Verify: workout_active goes off (or unavailable), END automation fires

### Test 4: Speed=0 without EndWorkout
- Start treadmill, exercise, stop treadmill using physical controls
- Wait for idle timeout (60s) + reconnect + second idle timeout
- Verify: workout_active goes off, session ends with correct data

### Test 5: Pause and resume
- Start treadmill, exercise, pause (speed=0), wait 50s, resume
- Verify: idle timer cancelled, session continues uninterrupted

### Test 6: EndWorkout during idle timeout reconnect
- Start treadmill, exercise, stop
- Time it so EndWorkout arrives during the reconnect phase
- Verify: session ends cleanly (not stuck on)

---

## Files Modified

- `custom_components/ftms/__init__.py` — Fixes 1-5 (all changes)
- No other files need modification
- HA automations: no changes needed (they react, don't control)
