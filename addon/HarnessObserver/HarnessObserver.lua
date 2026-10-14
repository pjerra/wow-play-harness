-- HarnessObserver.lua (WoW 1.12 API, Lua 5.0)
local function pct(cur, max) if not max or max == 0 then return 0 end return math.floor(cur * 100 / max) end

-- WoW writes Logs\WoWChatLog.txt and WoWCombatLog.txt only at logout. HXDB keeps the same lines
-- (same "M/D HH:MM:SS.mmm  text" format) and the client writes it on ReloadUI, which the harness triggers.
local CAP = 5000
local function ts()
  local ms = math.mod(math.floor(GetTime() * 1000), 1000)
  return date("%m/%d %H:%M:%S") .. string.format(".%03d", ms)
end
local HX_PixPush -- the live pixel channel (below); set once the strip exists
local function push(key, text)
  if HX_PixPush then HX_PixPush(key, ts() .. "  " .. text) end
  if not HXDB or not HXDB[key] then return end
  local ring = HXDB[key]
  table.insert(ring, ts() .. "  " .. text)
  if table.getn(ring) > CAP then
    table.remove(ring, 1)
    HXDB[key .. "_first"] = HXDB[key .. "_first"] + 1
  end
end

local function out(msg)
  DEFAULT_CHAT_FRAME:AddMessage(msg)
  push("chat", msg)
end

-- Lua errors: record them as HX|lua-error lines instead of the modal error popup, which steals the
-- keyboard from the harness (typed commands then go nowhere). Same message, first 300 chars.
local luaErrors = 0
seterrorhandler(function(msg)
  luaErrors = luaErrors + 1
  local text = string.sub(tostring(msg or "?"), 1, 300)
  DEFAULT_CHAT_FRAME:AddMessage("|cffff4040HX|lua-error|" .. text .. "|r")
  push("chat", "HX|lua-error|" .. text)
end)
function HX_LuaErrors() out("HX|lua-errors=" .. luaErrors) end

local COMBAT = {
  "CHAT_MSG_COMBAT_SELF_HITS", "CHAT_MSG_COMBAT_SELF_MISSES", "CHAT_MSG_COMBAT_PET_HITS", "CHAT_MSG_COMBAT_PET_MISSES",
  "CHAT_MSG_COMBAT_PARTY_HITS", "CHAT_MSG_COMBAT_PARTY_MISSES",
  "CHAT_MSG_COMBAT_FRIENDLYPLAYER_HITS", "CHAT_MSG_COMBAT_FRIENDLYPLAYER_MISSES",
  "CHAT_MSG_COMBAT_HOSTILEPLAYER_HITS", "CHAT_MSG_COMBAT_HOSTILEPLAYER_MISSES",
  "CHAT_MSG_COMBAT_CREATURE_VS_SELF_HITS", "CHAT_MSG_COMBAT_CREATURE_VS_SELF_MISSES",
  "CHAT_MSG_COMBAT_CREATURE_VS_PARTY_HITS", "CHAT_MSG_COMBAT_CREATURE_VS_PARTY_MISSES",
  "CHAT_MSG_COMBAT_CREATURE_VS_CREATURE_HITS", "CHAT_MSG_COMBAT_CREATURE_VS_CREATURE_MISSES",
  "CHAT_MSG_COMBAT_FRIENDLY_DEATH", "CHAT_MSG_COMBAT_HOSTILE_DEATH", "CHAT_MSG_COMBAT_XP_GAIN",
  "CHAT_MSG_SPELL_SELF_DAMAGE", "CHAT_MSG_SPELL_SELF_BUFF", "CHAT_MSG_SPELL_PET_DAMAGE", "CHAT_MSG_SPELL_PET_BUFF",
  "CHAT_MSG_SPELL_PARTY_DAMAGE", "CHAT_MSG_SPELL_PARTY_BUFF",
  "CHAT_MSG_SPELL_FRIENDLYPLAYER_DAMAGE", "CHAT_MSG_SPELL_FRIENDLYPLAYER_BUFF",
  "CHAT_MSG_SPELL_HOSTILEPLAYER_DAMAGE", "CHAT_MSG_SPELL_HOSTILEPLAYER_BUFF",
  "CHAT_MSG_SPELL_CREATURE_VS_SELF_DAMAGE", "CHAT_MSG_SPELL_CREATURE_VS_SELF_BUFF",
  "CHAT_MSG_SPELL_CREATURE_VS_PARTY_DAMAGE", "CHAT_MSG_SPELL_CREATURE_VS_PARTY_BUFF",
  "CHAT_MSG_SPELL_CREATURE_VS_CREATURE_DAMAGE", "CHAT_MSG_SPELL_CREATURE_VS_CREATURE_BUFF",
  "CHAT_MSG_SPELL_PERIODIC_SELF_DAMAGE", "CHAT_MSG_SPELL_PERIODIC_SELF_BUFFS",
  "CHAT_MSG_SPELL_PERIODIC_PARTY_DAMAGE", "CHAT_MSG_SPELL_PERIODIC_PARTY_BUFFS",
  "CHAT_MSG_SPELL_PERIODIC_FRIENDLYPLAYER_DAMAGE", "CHAT_MSG_SPELL_PERIODIC_FRIENDLYPLAYER_BUFFS",
  "CHAT_MSG_SPELL_PERIODIC_HOSTILEPLAYER_DAMAGE", "CHAT_MSG_SPELL_PERIODIC_HOSTILEPLAYER_BUFFS",
  "CHAT_MSG_SPELL_PERIODIC_CREATURE_DAMAGE", "CHAT_MSG_SPELL_PERIODIC_CREATURE_BUFFS",
  "CHAT_MSG_SPELL_AURA_GONE_SELF", "CHAT_MSG_SPELL_AURA_GONE_PARTY", "CHAT_MSG_SPELL_AURA_GONE_OTHER",
  "CHAT_MSG_SPELL_BREAK_AURA", "CHAT_MSG_SPELL_DAMAGESHIELDS_ON_SELF", "CHAT_MSG_SPELL_DAMAGESHIELDS_ON_OTHERS",
  "CHAT_MSG_SPELL_ITEM_ENCHANTMENTS",
}
-- chat event -> how WoWChatLog.txt shows it (%s = author)
local CHAT = {
  CHAT_MSG_SYSTEM = "", CHAT_MSG_SAY = "[%s] says: ", CHAT_MSG_YELL = "[%s] yells: ",
  CHAT_MSG_PARTY = "[Party] [%s]: ", CHAT_MSG_RAID = "[Raid] [%s]: ",
  CHAT_MSG_WHISPER = "[%s] whispers: ", CHAT_MSG_WHISPER_INFORM = "To [%s]: ",
  CHAT_MSG_LOOT = "",  -- "<name> receives loot", roll results (Need/Greed/Pass)
}

local retargetAt = nil -- set after a reload; the target is restored half a second later
local rec = CreateFrame("Frame")
rec:RegisterEvent("VARIABLES_LOADED")
rec:RegisterEvent("PLAYER_LOGOUT")
rec:RegisterEvent("PLAYER_ENTERING_WORLD")
for _, e in ipairs(COMBAT) do rec:RegisterEvent(e) end
for e in pairs(CHAT) do rec:RegisterEvent(e) end
rec:SetScript("OnEvent", function()
  if event == "PLAYER_LOGOUT" then
    -- Fired on ReloadUI too: remember the target so the harness's log flush does not lose it.
    if HXDB then
      HXDB.reloadTarget = UnitExists("target") and UnitName("target") or nil
      HXDB.reloadTargetHostile = UnitExists("target") and UnitCanAttack("player", "target") and 1 or nil
    end
  elseif event == "PLAYER_ENTERING_WORLD" then
    retargetAt = GetTime() + 0.5
  elseif event == "VARIABLES_LOADED" then
    HXDB = HXDB or {}
    for _, k in ipairs({ "chat", "combat" }) do
      HXDB[k] = HXDB[k] or {}
      HXDB[k .. "_first"] = HXDB[k .. "_first"] or 1
    end
  elseif CHAT[event] then
    push("chat", string.format(CHAT[event], arg2 or "?") .. (arg1 or ""))
  else
    push("combat", arg1 or "")
  end
end)

-- Target the nearest LIVING enemy (optionally with this exact name). TargetByName picks corpses too,
-- which made party bots answer "<mob> is dead" in tests. Cycles TargetNearestEnemy a few times.
function HX_TargetLive(name)
  for i = 1, 12 do
    TargetNearestEnemy()
    if UnitExists("target") and not UnitIsDead("target") and (not name or UnitName("target") == name) then
      out("HX|target=" .. UnitName("target"))
      return
    end
  end
  ClearTarget()
  out("HX|target=none")
end

function HX_Ready() out("HX|ready|" .. (UnitName("player") or "?")) end
-- A marker line the tester can search for (e.g. "pull starts here"), and the frame rate.
function HX_Mark(text) out("HX|mark|" .. tostring(text)) end
function HX_Fps() out("HX|fps=" .. string.format("%.1f", GetFramerate())) end
-- Pixel-channel load test: n numbered lines of about len characters each.
function HX_PixTest(n, len)
  local pad = string.rep("x", math.max(0, (len or 80) - 30))
  for i = 1, (n or 100) do out("HX|pixtest|" .. i .. "|" .. pad) end
end

-- Probe helpers, so testers type short /script lines (chat input is cut at 255 chars).
-- Bags: every item with its count, one line.
function HX_Bags()
  local items = {}
  for b = 0, 4 do
    for i = 1, GetContainerNumSlots(b) do
      local link = GetContainerItemLink(b, i)
      if link then
        local _, n = GetContainerItemInfo(b, i)
        local _, _, name = string.find(link, "%[(.+)%]")
        table.insert(items, (name or "?") .. "x" .. (n or 1))
      end
    end
  end
  out("HX|bags=" .. table.concat(items, ","))
end

-- Raid marks on the target and on each party member's target (1=Star ... 5=Moon, 8=Skull).
function HX_Marks()
  local s = "HX|marks"
  local function one(u)
    if UnitExists(u) then
      s = s .. "|" .. u .. "=" .. (UnitName(u) or "?") .. ":" .. (GetRaidTargetIndex(u) or 0)
        .. (UnitIsDead(u) and ":dead" or "") .. (UnitAffectingCombat(u) and ":combat" or "")
    end
  end
  one("target")
  for i = 1, GetNumPartyMembers() do one("party" .. i .. "target") end
  out(s)
end

-- One party member's buffs/debuffs by texture (1.12 has no names without a tooltip scan).
function HX_Auras(unit)
  unit = unit or "target"
  local b, d = {}, {}
  for i = 1, 16 do local t = UnitBuff(unit, i); if t then table.insert(b, (string.gsub(t, ".*\\", ""))) end end
  for i = 1, 16 do local t, c = UnitDebuff(unit, i); if t then table.insert(d, string.gsub(t, ".*\\", "") .. "x" .. (c or 1)) end end
  out("HX|auras|" .. unit .. "|buffs=" .. table.concat(b, ",") .. "|debuffs=" .. table.concat(d, ","))
end

local casting = nil -- spell name while the player is casting (1.12 has no UnitCastingInfo)
local evt = CreateFrame("Frame")
evt:RegisterEvent("SPELLCAST_START")
evt:RegisterEvent("SPELLCAST_STOP")
evt:RegisterEvent("SPELLCAST_FAILED")
evt:RegisterEvent("SPELLCAST_INTERRUPTED")
evt:SetScript("OnEvent", function()
  if event == "SPELLCAST_START" then casting = arg1 else casting = nil end
end)

function HX_State()
  local x, y = GetPlayerMapPosition("player")
  local s = "HX|p=" .. (UnitName("player") or "?") .. "," .. pct(UnitHealth("player"), UnitHealthMax("player"))
    .. "," .. pct(UnitMana("player"), UnitManaMax("player")) .. "," .. string.format("%.3f,%.3f", x or 0, y or 0)
    .. "," .. (GetRealZoneText() or "?")
  if UnitExists("target") then
    s = s .. "|t=" .. (UnitName("target") or "?") .. "," .. pct(UnitHealth("target"), UnitHealthMax("target"))
      .. "," .. (UnitIsDead("target") and "dead" or "alive")
  end
  local party = {}
  for i = 1, GetNumPartyMembers() do
    local u = "party" .. i
    table.insert(party, (UnitName(u) or "?") .. ":" .. pct(UnitHealth(u), UnitHealthMax(u)))
  end
  if table.getn(party) > 0 then s = s .. "|party=" .. table.concat(party, ",") end
  if casting then s = s .. "|cast=" .. casting end
  out(s)
end

local ticker, acc = CreateFrame("Frame"), 0
ticker:Hide()
ticker:SetScript("OnUpdate", function() acc = acc + arg1; if acc >= 1 then acc = 0; HX_State() end end)

SLASH_HX1 = "/hx"
SlashCmdList["HX"] = function(msg)
  if msg == "on" then ticker:Show() elseif msg == "off" then ticker:Hide() else HX_State() end
end

-- After a ReloadUI: put back the target the player had (HX_TargetLive skips corpses for enemies).
local restore = CreateFrame("Frame")
restore:SetScript("OnUpdate", function()
  if not retargetAt or GetTime() < retargetAt then return end
  retargetAt = nil
  local name = HXDB and HXDB.reloadTarget
  if not name or UnitExists("target") then return end
  if HXDB.reloadTargetHostile then HX_TargetLive(name) else TargetByName(name) end
  out("HX|retarget=" .. (UnitExists("target") and UnitName("target") or "none"))
  HXDB.reloadTarget = nil
end)

-- Live pixel channel: the harness reads new chat/combat lines off the screen instead of forcing a
-- ReloadUI. A strip of COLS x ROWS solid-colour cells at the top-left of the screen shows one page
-- of the line stream; the decoder is wowharness/pixel.py (keep the two in step). Cell = 6 bits,
-- 2 bits (4 levels) per channel. Page = 6 sync cells, row markers, then 3 bytes per 4 cells:
-- 'H' 'X' ver cols(2) rows flags sess seq(2) off(4) len(2) drop ck(2), payload.
-- Lua 5.0: no bit operators and no %, so everything is math.floor / math.mod arithmetic.
local PIX_COLS, PIX_ROWS, PIX_CELL = 160, 3, 3 -- CELL in screen units (768 = screen height) => >= 3 px
local PIX_PAGE_S, PIX_IDLE_S = 0.1, 1.0 -- next page after 0.1 s; an empty heartbeat page after 1 s idle
local PIX_QUEUE_MAX = 262144 -- bytes waiting (~70 s at full rate); newer lines are dropped and counted beyond
local PIX_HEAD = 19
local PIX_CAP = math.floor((PIX_COLS * PIX_ROWS - 6 - (PIX_ROWS - 1)) / 4) * 3 - PIX_HEAD
local SYNC = { 63, 0, 63, 0, 21, 42 }
local KIND = { chat = "c", combat = "k", pose = "p" } -- pose: HX|pose lines, pixel strip only

local pq, pqh, pqt, pqpos, pqbytes = {}, 1, 0, 1, 0 -- queue of lines, head, tail, byte pos in head
local pdrop, pseq, poff = 0, 0, 0
local psess = math.mod(math.floor(GetTime() * 1000), 256)
local pcells, pshown = {}, {}

HX_PixPush = function(key, line)
  local s = (KIND[key] or "o") .. string.gsub(line, "\n", " ") .. "\n"
  local n = string.len(s)
  if pqbytes + n > PIX_QUEUE_MAX then pdrop = pdrop + 1; return end
  pqt = pqt + 1; pq[pqt] = s; pqbytes = pqbytes + n
end

local function take(n)
  local parts = {}
  while n > 0 and pqh <= pqt do
    local s = pq[pqh]
    local rem = string.len(s) - pqpos + 1
    if rem <= n then
      table.insert(parts, string.sub(s, pqpos)); n = n - rem
      pq[pqh] = nil; pqh = pqh + 1; pqpos = 1
    else
      table.insert(parts, string.sub(s, pqpos, pqpos + n - 1)); pqpos = pqpos + n; n = 0
    end
  end
  local out = table.concat(parts)
  pqbytes = pqbytes - string.len(out)
  return out
end

local strip = CreateFrame("Frame", "HXPixelStrip", nil)
strip:SetFrameStrata("TOOLTIP")
strip:SetFrameLevel(120)
strip:SetWidth(PIX_COLS * PIX_CELL)
strip:SetHeight(PIX_ROWS * PIX_CELL)
strip:SetPoint("TOPLEFT", WorldFrame, "TOPLEFT", 0, 0)
for i = 0, PIX_COLS * PIX_ROWS - 1 do
  local t = strip:CreateTexture(nil, "OVERLAY")
  t:SetWidth(PIX_CELL); t:SetHeight(PIX_CELL)
  t:SetPoint("TOPLEFT", strip, "TOPLEFT", math.mod(i, PIX_COLS) * PIX_CELL, -math.floor(i / PIX_COLS) * PIX_CELL)
  t:SetTexture(0, 0, 0)
  pcells[i] = t; pshown[i] = 0
end
local LVL = { [0] = 0, 1 / 3, 2 / 3, 1 }

local function paint(i, v)
  if pshown[i] == v then return end
  pshown[i] = v
  pcells[i]:SetTexture(LVL[math.floor(v / 16)], LVL[math.mod(math.floor(v / 4), 4)], LVL[math.mod(v, 4)])
end

-- Header + payload as a list of byte values (also used by the tests through HX_PixPageBytes).
function HX_PixPageBytes(flags, sess, seq, off, payload, drop)
  local n = string.len(payload)
  local b = { 72, 88, 1, math.floor(PIX_COLS / 256), math.mod(PIX_COLS, 256), PIX_ROWS, flags, sess,
    math.floor(seq / 256), math.mod(seq, 256),
    math.mod(math.floor(off / 16777216), 256), math.mod(math.floor(off / 65536), 256),
    math.mod(math.floor(off / 256), 256), math.mod(off, 256),
    math.floor(n / 256), math.mod(n, 256), math.mod(drop, 256) }
  for i = 1, n do b[17 + i] = string.byte(payload, i) end
  local s1, s2 = 0, 0
  for i = 1, 17 + n do s1 = math.mod(s1 + b[i], 255); s2 = math.mod(s2 + s1, 255) end
  -- checksum goes in as bytes 18-19, before the payload
  for i = 17 + n, 18, -1 do b[i + 2] = b[i] end
  b[18] = s2; b[19] = s1
  return b
end

-- Byte list -> cell values: sync cells, row markers (column 0 of rows 1.., black on odd rows, white
-- on even: the decoder's vertical pitch), then 4 cells per 3 bytes in DATA order, zeros after.
local DATA = {}
for i = 6, PIX_COLS * PIX_ROWS - 1 do
  if math.mod(i, PIX_COLS) ~= 0 then table.insert(DATA, i) end
end
function HX_PixCells(b)
  local cells, n, k = {}, table.getn(b), 1
  for i = 0, PIX_COLS * PIX_ROWS - 1 do cells[i] = 0 end
  for i = 1, 6 do cells[i - 1] = SYNC[i] end
  for r = 2, PIX_ROWS - 1, 2 do cells[r * PIX_COLS] = 63 end
  for i = 1, n, 3 do
    local v = b[i] * 65536 + (b[i + 1] or 0) * 256 + (b[i + 2] or 0)
    cells[DATA[k]] = math.floor(v / 262144); cells[DATA[k + 1]] = math.mod(math.floor(v / 4096), 64)
    cells[DATA[k + 2]] = math.mod(math.floor(v / 64), 64); cells[DATA[k + 3]] = math.mod(v, 64)
    k = k + 4
  end
  return cells
end

local function showPage()
  local flags = (pqpos == 1) and 1 or 0
  local payload = take(PIX_CAP)
  pseq = math.mod(pseq + 1, 65536)
  local cells = HX_PixCells(HX_PixPageBytes(flags, psess, pseq, poff, payload, pdrop))
  poff = poff + string.len(payload)
  for i = 0, PIX_COLS * PIX_ROWS - 1 do paint(i, cells[i]) end
end

local pacc, pidle = 0, 0
strip:SetScript("OnUpdate", function()
  pacc = pacc + arg1; pidle = pidle + arg1
  if pacc < PIX_PAGE_S then return end
  if pqh <= pqt or pidle >= PIX_IDLE_S then
    pacc, pidle = 0, 0
    showPage()
  end
end)
showPage()

-- /hx pix off|on hides or shows the strip (the harness then falls back to the ReloadUI path).
local pixWanted = true
local hxSlash = SlashCmdList["HX"]
SlashCmdList["HX"] = function(msg)
  if msg == "pix off" then pixWanted = false; strip:Hide()
  elseif msg == "pix on" then pixWanted = true; strip:Show()
  else hxSlash(msg) end
end
-- Seen live: the strip was found hidden after both clients' windows were re-created by a display
-- change (cause not proven). Unless /hx pix off asked for it, a watchdog under UIParent shows it
-- again and says so in the log.
local pixDog, dogAcc = CreateFrame("Frame", nil, UIParent), 0
pixDog:SetScript("OnUpdate", function()
  dogAcc = dogAcc + arg1
  if dogAcc < 0.5 then return end
  dogAcc = 0
  if pixWanted and not strip:IsShown() then strip:Show(); out("HX|pix=reshown") end
end)

-- Pose stream for the harness's walk_to: facing, and world position when the client has an API for it,
-- as "HX|pose=<GetTime>,<facing>,<x>,<y>,<z>,<mapx>,<mapy>" lines on the pixel strip only (not the chat
-- frame, not HXDB), every 0.1 s while on. Empty field = no source. 1.12 has no GetPlayerFacing; the
-- minimap's player arrow is a Model whose facing is the player's. UnitPosition exists only with client
-- mods (SuperWoW); without it, positions in instances come from the server (.gps).
local arrow
local function findArrow()
  if arrow then return arrow end
  local kids, unnamed, n = { Minimap:GetChildren() }, nil, 0
  for i = 1, table.getn(kids) do
    local c = kids[i]
    if c.IsObjectType and c:IsObjectType("Model") and not c:GetName() then
      if string.find(string.lower(tostring(c.GetModel and c:GetModel() or "")), "minimaparrow") then arrow = c; return c end
      unnamed, n = c, n + 1
    end
  end
  if n == 1 then arrow = unnamed end
  return arrow
end
local function facingSrc()
  if GetPlayerFacing then return "GetPlayerFacing" end
  if findArrow() then return "arrow" end
  return "none"
end
function HX_Facing()
  if GetPlayerFacing then return GetPlayerFacing() end
  local a = findArrow()
  if a then return a:GetFacing() end
end
local function opt(fmt, v) if v then return string.format(fmt, v) end return "" end
local function poseLine()
  local x, y, z
  if UnitPosition then x, y, z = UnitPosition("player") end
  local mx, my = GetPlayerMapPosition("player")
  return "HX|pose=" .. string.format("%.2f", GetTime()) .. "," .. opt("%.3f", HX_Facing()) .. "," .. opt("%.1f", x)
    .. "," .. opt("%.1f", y) .. "," .. opt("%.1f", z) .. "," .. opt("%.4f", mx) .. "," .. opt("%.4f", my)
end
local poseFrame, poseAcc, poseEvery, poseUntil = CreateFrame("Frame"), 0, 0.1, 0
poseFrame:Hide()
poseFrame:SetScript("OnUpdate", function()
  poseAcc = poseAcc + arg1
  if poseAcc < poseEvery then return end
  poseAcc = 0
  if GetTime() > poseUntil then poseFrame:Hide(); out("HX|pose=off|timeout"); return end
  if HX_PixPush then HX_PixPush("pose", poseLine()) end
end)
-- On for up to 900 s (a harness that died must not leave it running); calling again renews.
function HX_PoseOn(every)
  poseEvery = math.max(0.05, tonumber(every) or 0.1)
  poseUntil = GetTime() + 900
  poseFrame:Show()
end
function HX_PoseOff() poseFrame:Hide() end

-- walk_to begins/ends here: the server's .gps reports the SELECTED unit, so the target is put aside for
-- the walk and restored after it (like after a ReloadUI).
local walkTarget, walkHostile
function HX_WalkBegin(every)
  walkTarget = UnitExists("target") and UnitName("target") or nil
  walkHostile = UnitExists("target") and UnitCanAttack("player", "target")
  ClearTarget()
  HX_PoseOn(every)
  out("HX|walk=begin|target=" .. (walkTarget or "") .. "|facing=" .. facingSrc() .. "|pos="
    .. (UnitPosition and "UnitPosition" or "none"))
end
function HX_WalkEnd()
  HX_PoseOff()
  if walkTarget and not UnitExists("target") then
    if walkHostile then HX_TargetLive(walkTarget) else TargetByName(walkTarget) end
  end
  out("HX|walk=end|retarget=" .. (UnitExists("target") and UnitName("target") or "none"))
  walkTarget = nil
end

-- One line with every candidate position/facing API, for checking a client: /script HX_Probe()
function HX_Probe()
  local x, y, z
  if UnitPosition then x, y, z = UnitPosition("player") end
  local a = findArrow()
  local mx, my = GetPlayerMapPosition("player")
  out("HX|probe|UnitPosition=" .. (UnitPosition and (tostring(x) .. "," .. tostring(y) .. "," .. tostring(z)) or "nil")
    .. "|GetPlayerFacing=" .. (GetPlayerFacing and tostring(GetPlayerFacing()) or "nil")
    .. "|arrow=" .. (a and (tostring(a.GetModel and a:GetModel()) .. ":" .. tostring(a:GetFacing())) or "nil")
    .. "|map=" .. string.format("%.4f,%.4f", mx or 0, my or 0) .. "|zone=" .. (GetRealZoneText() or "?"))
end

local hxSlash2 = SlashCmdList["HX"]
SlashCmdList["HX"] = function(msg)
  if msg == "pose on" then HX_PoseOn() elseif msg == "pose off" then HX_PoseOff()
  elseif msg == "probe" then HX_Probe() else hxSlash2(msg) end
end
