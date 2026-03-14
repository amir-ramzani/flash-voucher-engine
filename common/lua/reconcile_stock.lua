-- Recompute stock from the claims hash after drift repair (e.g. claims restored from
-- DynamoDB after a Redis failover). Only ever LOWERS stock: raising it could re-open
-- vouchers that were already handed out.
--
-- KEYS[1] = meta, KEYS[2] = stock, KEYS[3] = claims
-- Returns {stock_before, stock_after}; {-1, -1} if the campaign is unknown.

local total = tonumber(redis.call('HGET', KEYS[1], 'totalStock') or '-1')
if total < 0 then
  return {-1, -1}
end
local expected = total - redis.call('HLEN', KEYS[3])
if expected < 0 then
  expected = 0
end
local current = tonumber(redis.call('GET', KEYS[2]) or '0')
if current > expected then
  redis.call('SET', KEYS[2], expected)
  return {current, expected}
end
return {current, current}
