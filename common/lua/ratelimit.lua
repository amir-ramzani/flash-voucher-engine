-- Fixed-window counter. KEYS[1] = ratelimit:<user>, ARGV[1] = window ms. Returns hit count.
local n = redis.call('INCR', KEYS[1])
if n == 1 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
return n
