-- 006_pin_lockout - a counter the cloud has had since 0001 and the till never did.
--
-- `authenticate-pin` rate-limits: ten attempts per code per five minutes, with
-- a comment saying what for. The terminal had nothing. Every offline PIN check
-- went to argon2 and back as often as anybody liked, and argon2id at the
-- shipped parameters verifies in 22.8 ms natively - a four-digit space in four
-- minutes, unattended, with the network unplugged.
--
-- Persisted rather than in-process, deliberately. The cloud's counter lives in
-- a Map and that is fine for a function that restarts; a local one that reset
-- when the app closed would protect nothing at all, since closing the app is
-- something the person guessing can do.
ALTER TABLE cached_users ADD COLUMN consecutive_pin_failures INTEGER NOT NULL DEFAULT 0;

-- Null means not locked. Nothing clears this on a timer; it is compared
-- against the clock at the moment somebody tries.
ALTER TABLE cached_users ADD COLUMN pin_locked_until TEXT;
