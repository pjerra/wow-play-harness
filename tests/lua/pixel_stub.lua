-- Runs HarnessObserver.lua under plain Lua 5.1 with a stubbed WoW API and prints the pixel strip.
-- usage: lua5.1 pixel_stub.lua <addon.lua> <events file>
-- events file lines: "EVENT_NAME<TAB>arg1<TAB>arg2" fires an event; "TICK<TAB>seconds" runs OnUpdate
-- and prints "PAGE <cell values>" for the strip (each texture's r,g,b back to a 0..63 value);
-- "LUA<TAB>code" runs code (more stubs, or a /script line).
local now = 1000.25
function GetTime() return now end
function date(f) return "10/12 10:00:00" end
function seterrorhandler(f) end
function UnitName() return "Tester" end
DEFAULT_CHAT_FRAME = { AddMessage = function() end }
SlashCmdList = {}
local frames, textures = {}, {}
local function obj(kind)
  local o = { scripts = {}, shown = true }
  setmetatable(o, { __index = function(t, k) return function() end end })
  o.SetScript = function(self, name, fn) self.scripts[name] = fn end
  o.Hide = function(self) self.shown = false end
  o.Show = function(self) self.shown = true end
  o.IsShown = function(self) return self.shown end
  o.CreateTexture = function(self)
    local t = obj("texture"); t.rgb = { 0, 0, 0 }
    t.SetTexture = function(tt, r, g, b) tt.rgb = { r, g, b } end
    table.insert(textures, t); return t
  end
  return o
end
function CreateFrame() local f = obj("frame"); table.insert(frames, f); return f end
WorldFrame = obj("frame")
dofile(arg[1])
local function tick(dt)
  now = now + dt
  for _, f in ipairs(frames) do
    if f.scripts.OnUpdate and f.shown then arg1 = dt; f.scripts.OnUpdate() end
  end
  local out = {}
  for i, t in ipairs(textures) do
    local function l(x) return math.floor(x * 3 + 0.5) end
    out[i] = l(t.rgb[1]) * 16 + l(t.rgb[2]) * 4 + l(t.rgb[3])
  end
  print("PAGE " .. table.concat(out, " "))
end
for line in io.lines(arg[2]) do
  local _, _, ev, a1, a2 = string.find(line, "^([^\t]+)\t?([^\t]*)\t?(.*)$")
  if ev == "TICK" then tick(tonumber(a1))
  elseif ev == "LUA" then assert(loadstring(a1))()
  else
    event, arg1, arg2 = ev, a1, a2
    for _, f in ipairs(frames) do if f.scripts.OnEvent then f.scripts.OnEvent() end end
  end
end
