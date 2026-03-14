-- Atomic check-then-act claim. Redis runs the whole script with no other command in between.
--
-- KEYS[1] = {campaign:<id>}:meta    hash  totalStock, startAt, endAt (epoch ms)
-- KEYS[2] = {campaign:<id>}:stock   string int
-- KEYS[3] = {campaign:<id>}:claims  hash  userId -> voucherCode | REVOKED
-- KEYS[4] = {campaign:<id>}:events  stream
-- ARGV[1] = user id
-- ARGV[2] = freshly generated voucher code (used only if this call wins)
--
-- Returns {status, value}:
--   {'WON', code} | {'ALREADY', code-or-REVOKED} | {'SOLD_OUT', ''}
--   {'NOT_STARTED', ''} | {'ENDED', ''} | {'NO_CAMPAIGN', ''}

-- 1. Idempotency first: a returning winner always gets their original code back,
--    even after the window closes or stock runs out.
local existing = redis.call('HGET', KEYS[3], ARGV[1])
if existing then
  return {'ALREADY', existing}
end

-- 2. Campaign window, checked against Redis' clock so every API instance agrees.
local window = redis.call('HMGET', KEYS[1], 'startAt', 'endAt')
if not window[1] then
  return {'NO_CAMPAIGN', ''}
end
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
if now < tonumber(window[1]) then
  return {'NOT_STARTED', ''}
end
if now >= tonumber(window[2]) then
  return {'ENDED', ''}
end

-- 3. Stock.
if tonumber(redis.call('GET', KEYS[2]) or '0') <= 0 then
  return {'SOLD_OUT', ''}
end

-- 4. Act: decrement, record, emit — all inside the same atomic unit.
redis.call('DECR', KEYS[2])
redis.call('HSET', KEYS[3], ARGV[1], ARGV[2])
redis.call('XADD', KEYS[4], '*', 'user', ARGV[1], 'code', ARGV[2], 'ts', tostring(now))
return {'WON', ARGV[2]}
