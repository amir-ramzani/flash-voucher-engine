-- Seed a campaign. Safe to re-run: stock is only set if absent, so a retried
-- POST /campaigns can never refill stock mid-sale.
--
-- KEYS[1] = {campaign:<id>}:meta
-- KEYS[2] = {campaign:<id>}:stock
-- ARGV[1] = totalStock, ARGV[2] = startAt ms, ARGV[3] = endAt ms
-- Returns current stock.

redis.call('HSET', KEYS[1], 'totalStock', ARGV[1], 'startAt', ARGV[2], 'endAt', ARGV[3])
redis.call('SET', KEYS[2], ARGV[1], 'NX')
return tonumber(redis.call('GET', KEYS[2]))
