#!/usr/bin/env python3
"""
ApexMech v30.0 - Fight-When-Even, Retreat-When-Outnumbered, Heal-Between (mechfight/1)
Built on v25 (v18 aggression + regen heal/hide + laser dodge) with v29's A* routing and cube farm.
Fixes vs v29:
  - flee only when genuinely outnumbered (3+ close, or 2+ while hurt); a lone hunter is fought
  - flee has an end: a stand-and-fight valve, and a real heal/hide state (45 HP/s regen)
  - lightning + gun is the main loadout (laser only as fallback, played as a corner-peeker)
  - cube farm capped at the 10-cube stat cap
  - crate/NPC farm fallback fixed (both considered, nearest wins)
  - flee fallback guarded against empty threat lists
Standard library only.
"""
import heapq
import json
import math
import os
import random
import mechfight as mf

# ---------------- TUNING / A-B FLAGS ----------------
HEAL_HIDE = True
HEAL_ENTER_FRAC = 0.45
HEAL_EXIT_FRAC = 0.88
RETREAT_ENABLED = True          # retreat when outnumbered
STAND_AFTER_S = 7.0             # retreating this long with enemies still on us -> turn and fight
STAND_FOR_S = 5.0
SNOWBALL_CUBES = 10             # = stat cap; stop farming for stats at this many
FARM_MIN_ENEMY_DIST = 75.0      # farm NPCs/crates only if no mech is closer than this
KITE_IDEAL_DIST = 85.0          # laser corner-peek standoff
LASER_DODGE = True
AVOID_MORTAR = True
DROP_HOLD = True
LOGGING = True
DEBUG_DRAW = True
# ----------------------------------------------------

TWO_PI = 6.283185307179586
DIRS_12 = tuple((math.cos(i * (TWO_PI / 12.0)), math.sin(i * (TWO_PI / 12.0))) for i in range(12))


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def wrap_angle(a: float) -> float:
    while a > math.pi:
        a -= TWO_PI
    while a < -math.pi:
        a += TWO_PI
    return a


def angle_diff(target: float, current: float) -> float:
    return wrap_angle(target - current)


def dist2d(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def dist3d(a, b) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def point_in_ring(x: float, y: float, ring) -> bool:
    inside = False
    n = len(ring) // 2
    j = n - 1
    for i in range(n):
        xi, yi = ring[2 * i], ring[2 * i + 1]
        xj, yj = ring[2 * j], ring[2 * j + 1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


class ArenaMap:
    """Collision + LOS + 18 m A* grid + evade/peek scoring."""

    def __init__(self, map_dir: str):
        self.cell_size = 45.0
        self.grid: dict[tuple[int, int], list[int]] = {}
        self.prisms: list[tuple] = []
        self.bounds = (-508.0, 551.0, -329.0, 384.0)
        self.nav_step = 18.0
        self.nav_blocked: set[tuple[int, int]] = set()

        world_path = os.path.join(map_dir, "v2", "world.json")
        if os.path.exists(world_path):
            try:
                with open(world_path, "r", encoding="utf-8") as f:
                    world = json.load(f)
                b = world.get("bounds", {})
                if b:
                    self.bounds = (
                        float(b.get("minX", -512.0)) + 4.0,
                        float(b.get("maxX", 555.0)) - 4.0,
                        float(b.get("minY", -333.0)) + 4.0,
                        float(b.get("maxY", 388.0)) - 4.0,
                    )
                for p in world.get("prisms", []):
                    ring = p.get("ring", [])
                    if len(ring) < 6:
                        continue
                    xs = ring[0::2]
                    ys = ring[1::2]
                    minx, maxx = min(xs), max(xs)
                    miny, maxy = min(ys), max(ys)
                    prism = (
                        float(p.get("base", -100.0)),
                        float(p.get("top", 100.0)),
                        minx, miny, maxx, maxy,
                        ring,
                        p.get("holes", []),
                    )
                    idx = len(self.prisms)
                    self.prisms.append(prism)
                    cs = self.cell_size
                    for cx in range(int(math.floor(minx / cs)), int(math.floor(maxx / cs)) + 1):
                        for cy in range(int(math.floor(miny / cs)), int(math.floor(maxy / cs)) + 1):
                            self.grid.setdefault((cx, cy), []).append(idx)
                self._build_nav_grid()
            except Exception as e:
                mf.log(f"[map] load error: {e}")

    def _build_nav_grid(self):
        minx, maxx, miny, maxy = self.bounds
        step = self.nav_step
        for gx in range(int(math.floor(minx / step)), int(math.floor(maxx / step)) + 1):
            wx = (gx + 0.5) * step
            for gy in range(int(math.floor(miny / step)), int(math.floor(maxy / step)) + 1):
                wy = (gy + 0.5) * step
                if self.is_blocked_body(wx, wy, 0.5):
                    self.nav_blocked.add((gx, gy))

    def point_in_prism(self, x: float, y: float, z: float) -> bool:
        cand = self.grid.get((int(math.floor(x / 45.0)), int(math.floor(y / 45.0))))
        if not cand:
            return False
        prisms = self.prisms
        for idx in cand:
            base, top, minx, miny, maxx, maxy, ring, holes = prisms[idx]
            if z < base or z > top or x < minx or x > maxx or y < miny or y > maxy:
                continue
            if point_in_ring(x, y, ring):
                if not holes or not any(point_in_ring(x, y, h) for h in holes):
                    return True
        return False

    def is_blocked_body(self, x: float, y: float, z_feet: float) -> bool:
        b = self.bounds
        if x < b[0] or x > b[1] or y < b[2] or y > b[3]:
            return True
        cand = self.grid.get((int(math.floor(x / 45.0)), int(math.floor(y / 45.0))))
        if not cand:
            return False
        z_top = z_feet + 9.0
        prisms = self.prisms
        for idx in cand:
            base, top, minx, miny, maxx, maxy, ring, holes = prisms[idx]
            if z_top <= base or z_feet >= top - 1.2 or x < minx or x > maxx or y < miny or y > maxy:
                continue
            if point_in_ring(x, y, ring):
                if not holes or not any(point_in_ring(x, y, h) for h in holes):
                    return True
        return False

    def has_los(self, a, b, max_steps: int = 12) -> bool:
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        dz = b[2] - a[2]
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        if d < 6.0:
            return True
        steps = min(int(d / 10.0), max_steps)
        if steps < 2:
            return True
        sx, sy, sz = dx / steps, dy / steps, dz / steps
        x, y, z = a[0] + sx, a[1] + sy, a[2] + sz
        for _ in range(1, steps):
            if self.point_in_prism(x, y, z):
                return False
            x += sx
            y += sy
            z += sz
        return True

    def find_path_waypoint(self, pos, goal_xy, max_nodes: int = 140):
        step = self.nav_step
        sx, sy = int(math.floor(pos[0] / step)), int(math.floor(pos[1] / step))
        gx, gy = int(math.floor(goal_xy[0] / step)), int(math.floor(goal_xy[1] / step))
        if (sx, sy) == (gx, gy):
            return (goal_xy[0], goal_xy[1])

        start = (sx, sy)
        goal = (gx, gy)
        open_h = [(0.0, start)]
        came_from: dict[tuple[int, int], tuple[int, int]] = {}
        g_score: dict[tuple[int, int], float] = {start: 0.0}
        blocked = self.nav_blocked
        best_node = start
        best_h = math.hypot(gx - sx, gy - sy)

        expansions = 0
        while open_h and expansions < max_nodes:
            _, cur = heapq.heappop(open_h)
            if cur == goal:
                best_node = cur
                break
            expansions += 1
            cx, cy = cur
            cg = g_score[cur]
            for dx, dy, cost in (
                (1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414),
            ):
                nxt = (cx + dx, cy + dy)
                if nxt != goal and nxt in blocked:
                    continue
                ng = cg + cost
                if ng < g_score.get(nxt, 1e9):
                    g_score[nxt] = ng
                    h = math.hypot(gx - nxt[0], gy - nxt[1])
                    if h < best_h:
                        best_h = h
                        best_node = nxt
                    heapq.heappush(open_h, (ng + h * 1.15, nxt))
                    came_from[nxt] = cur

        node = best_node
        path = []
        while node in came_from:
            path.append(node)
            node = came_from[node]
        if not path:
            return (goal_xy[0], goal_xy[1])
        path.reverse()
        pick = path[min(1, len(path) - 1)]
        return ((pick[0] + 0.5) * step, (pick[1] + 0.5) * step)

    def steer_around_walls(self, pos, desired_dx: float, desired_dy: float):
        mag = math.hypot(desired_dx, desired_dy)
        if mag < 1e-4:
            return 0.0, 0.0
        base_ang = math.atan2(desired_dy, desired_dx)
        x, y, z = pos[0], pos[1], pos[2]
        for off in (0.0, 0.4, -0.4, 0.8, -0.8, 1.25, -1.25, 1.75, -1.75, 2.25, -2.25):
            ang = base_ang + off
            ca, sa = math.cos(ang), math.sin(ang)
            if not self.is_blocked_body(x + ca * 4.8, y + sa * 4.8, z) and not self.is_blocked_body(x + ca * 9.0, y + sa * 9.0, z):
                return ca, sa
        return desired_dx / mag, desired_dy / mag

    def evade_direction(self, pos, muzzle_h: float, threats, zc, zr: float, prev_dir):
        """Break LOS from threats, gain distance, stay in zone, hysteresis vs jitter."""
        x, y, z = pos[0], pos[1], pos[2]
        safe_r = max(zr - 12.0, 8.0)
        best_s, best_d = -1e9, None
        for i in range(16):
            a = i * (TWO_PI / 16)
            ca, sa = math.cos(a), math.sin(a)
            if self.is_blocked_body(x + ca * 5.0, y + sa * 5.0, z):
                continue
            nx, ny = x + ca * 14.0, y + sa * 14.0
            if self.is_blocked_body(nx, ny, z):
                continue
            cm = (nx, ny, z + muzzle_h)
            s = 0.0
            for e in threats[:5]:
                ep = e["pos"]
                old_d = math.hypot(x - ep[0], y - ep[1])
                new_d = math.hypot(nx - ep[0], ny - ep[1])
                if new_d < 170.0:
                    if self.has_los(cm, (ep[0], ep[1], ep[2] + 4.5), max_steps=7):
                        s -= 60.0 + max(0.0, 80.0 - new_d) * 0.8
                    else:
                        s += 45.0
                s += clamp(new_d - old_d, -10.0, 14.0) * 1.5
            dz = math.hypot(nx - zc[0], ny - zc[1])
            if dz > safe_r:
                s -= (dz - safe_r) * 3.0
            if prev_dir is not None and ca * prev_dir[0] + sa * prev_dir[1] > 0.7:
                s += 15.0
            if s > best_s:
                best_s, best_d = s, (ca, sa)
        return best_d

    def find_cover_and_peek(self, pos, muzzle_h: float, target, secondaries, want_peek: bool, zc, zr: float):
        """Laser corner-peek: hide while charging, step out to fire."""
        x, y, z = pos[0], pos[1], pos[2]
        tp = target["pos"]
        t_aim = (tp[0], tp[1], tp[2] + 4.5)
        safe_r = max(zr - 10.0, 8.0)
        best_score = -1e9
        best_pt = None
        for rad in (10.0, 18.0):
            for ca, sa in DIRS_12:
                nx, ny = x + ca * rad, y + sa * rad
                if self.is_blocked_body(x + ca * 5.0, y + sa * 5.0, z) or self.is_blocked_body(nx, ny, z):
                    continue
                cm = (nx, ny, z + muzzle_h)
                sees_t = self.has_los(cm, t_aim, max_steps=8)
                td = math.hypot(nx - tp[0], ny - tp[1])
                score = 0.0
                if want_peek:
                    score += 85.0 if sees_t else -70.0
                else:
                    score += 110.0 if not sees_t else -95.0
                score -= abs(td - KITE_IDEAL_DIST) * 0.45
                score -= rad * 0.9
                for se in secondaries[:3]:
                    sp = se["pos"]
                    sd = math.hypot(nx - sp[0], ny - sp[1])
                    if sd < 160.0:
                        if self.has_los(cm, (sp[0], sp[1], sp[2] + 4.5), max_steps=6):
                            score -= 90.0 + max(0.0, 75.0 - sd) * 0.8
                        else:
                            score += 35.0
                dz = math.hypot(nx - zc[0], ny - zc[1])
                if dz > safe_r:
                    score -= (dz - safe_r) * 3.0
                if score > best_score:
                    best_score = score
                    best_pt = (nx, ny)
        return best_pt


class Bot:
    name = "ApexMech-v30.0"

    def __init__(self, hello: mf.Hello):
        self.slot = hello["slot"]
        self.rng = random.Random(hello["seed"])
        self.config = hello["config"]
        self.map = ArenaMap(hello["map"]["dir"])
        self.pickup_r = self.config.get("loot", {}).get("pickup_radius", 4.0)
        vis = self.config.get("vision", {})
        self.fov_half = math.radians(float(vis.get("fov_deg", 120.0))) * 0.5
        self.vis_range = float(vis.get("range", 200.0))
        self.near_r = float(vis.get("near_range", 45.0))

        self.weapon_rank = {"lightning": 5, "gun": 4, "laser": 3, "mortar": 1}
        self.upgrade_weights = {
            "armor": 100,
            "lightning": 98,
            "hp": 97,
            "gun_rate": 95,
            "gun_damage": 94,
            "speed": 92,
            "regen": 91,
            "reload": 88,
            "boost": 85,
            "energy": 83,
            "laser": 80,
            "sensors": 62,
            "mortar": 35,
        }

        self.last_pos = None
        self.stuck_ticks = 0
        self.strafe_dir = 1.0
        self.strafe_timer = 0
        self.strafe_period = 7
        self.last_damage_bearing = None
        self.last_damage_t = -10.0
        self.last_hit_t = -10.0
        self.roam_target = None

        self.heal = False
        self.heal_since = 0.0
        self.flee_until = 0.0
        self.flee_since = None
        self.stand_until = 0.0
        self.prev_evade_dir = None

        self.path_wp = None
        self.path_goal = None
        self.path_tick = -100
        self._last_log = -9.0

    # ------------------------------------------------------------------ helpers
    def pick_upgrade(self, offer) -> int:
        best_i, best_score = 0, -1e9
        for i, ch in enumerate(offer.get("choices", [])):
            uid = ch.get("id", "").lower()
            desc = ch.get("desc", "").lower()
            score = 50
            for k, w in self.upgrade_weights.items():
                if k in uid or k in desc:
                    score = max(score, w)
            if score > best_score:
                best_score, best_i = score, i
        return best_i

    def aim_at_point(self, muzzle_pos, target_pos):
        dx = target_pos[0] - muzzle_pos[0]
        dy = target_pos[1] - muzzle_pos[1]
        dz = target_pos[2] - muzzle_pos[2]
        return math.atan2(dy, dx), clamp(math.atan2(dz, max(math.hypot(dx, dy), 1e-4)), -1.2, 1.2)

    def lead_target(self, muzzle_pos, target_pos, target_vel, proj_speed: float):
        d = dist3d(muzzle_pos, target_pos)
        t_flight = d / max(proj_speed, 1.0)
        px = target_pos[0] + target_vel[0] * t_flight
        py = target_pos[1] + target_vel[1] * t_flight
        pz = target_pos[2] + target_vel[2] * t_flight
        t_flight = dist3d(muzzle_pos, (px, py, pz)) / max(proj_speed, 1.0)
        return (
            target_pos[0] + target_vel[0] * t_flight,
            target_pos[1] + target_vel[1] * t_flight,
            target_pos[2] + target_vel[2] * t_flight,
        )

    def enemy_sees_me(self, e, pos) -> bool:
        ep = e["pos"]
        d = dist2d(pos, ep)
        if d > self.vis_range:
            return False
        if not self.map.has_los((ep[0], ep[1], ep[2] + 8.0), (pos[0], pos[1], pos[2] + 4.5), max_steps=10):
            return False
        if d <= self.near_r:
            return True
        yaw = e.get("aim", [0.0, 0.0])[0]
        bearing = math.atan2(pos[1] - ep[1], pos[0] - ep[0])
        return abs(angle_diff(bearing, yaw)) <= self.fov_half

    def laser_dodge(self, pos, enemies):
        for e in enemies:
            if not e.get("charging"):
                continue
            ep = e["pos"]
            d = dist2d(pos, ep)
            if d > 285.0 or d < 3.0:
                continue
            yaw = e.get("aim", [0.0, 0.0])[0]
            dx, dy = math.cos(yaw), math.sin(yaw)
            rx, ry = pos[0] - ep[0], pos[1] - ep[1]
            if rx * dx + ry * dy <= 0.0:
                continue
            lat = -rx * dy + ry * dx
            if abs(lat) < 7.0:
                sgn = 1.0 if lat >= 0.0 else -1.0
                if abs(lat) < 0.3:
                    sgn = self.strafe_dir
                return (-dy * sgn, dx * sgn)
        return None

    def get_astar_dir(self, tick: int, pos, goal_xy):
        if (
            self.path_wp is None
            or self.path_goal is None
            or (tick - self.path_tick) >= 10
            or dist2d(goal_xy, self.path_goal) > 22.0
            or dist2d(pos, self.path_wp) < 8.0
        ):
            self.path_wp = self.map.find_path_waypoint(pos, goal_xy, max_nodes=140)
            self.path_goal = (goal_xy[0], goal_xy[1])
            self.path_tick = tick
        return self.path_wp[0] - pos[0], self.path_wp[1] - pos[1]

    # --------------------------------------------------------------------- act
    def act(self, obs: mf.Observation) -> mf.Action:
        tick = obs["tick"]
        t = obs["t"]
        me = obs["self"]
        pos = me["pos"]
        hp_frac = me["hp"] / max(me["max_hp"], 1.0)
        energy = me["energy"]
        weapons = me["weapons"]
        my_cubes = me.get("cubes", 0)
        stats = me.get("stats", {})
        muzzle_h = stats.get("muzzle_height", 7.0)
        muzzle_pos = (pos[0], pos[1], pos[2] + muzzle_h)
        cur_yaw, _ = me["aim"]

        took_hit = False
        for ev in obs.get("events", []):
            if ev.get("type") == "damaged":
                took_hit = True
                if ev.get("weapon") != "zone":
                    self.last_hit_t = t
                if ev.get("bearing") is not None:
                    self.last_damage_bearing = ev["bearing"]
                    self.last_damage_t = t

        if self.last_pos is not None and dist2d(pos, self.last_pos) < 0.25:
            self.stuck_ticks += 1
        else:
            self.stuck_ticks = 0
        self.last_pos = pos

        self.strafe_timer += 1
        if self.strafe_timer >= self.strafe_period or (took_hit and self.strafe_timer >= 3):
            self.strafe_timer = 0
            self.strafe_dir = -self.strafe_dir
            self.strafe_period = self.rng.randint(6, 9)

        action: mf.Action = {
            "tick": tick,
            "move": [0.0, 0.0],
            "boost": False,
            "jump": False,
            "aim": [cur_yaw, 0.0],
            "fire": [False, False],
            "reload": False,
        }

        # 1. Upgrade offer
        if obs.get("offer") is not None:
            action["pick"] = self.pick_upgrade(obs["offer"])

        # 2. Weapon pickup / swap (lightning + gun is the target loadout)
        held_kinds = {w["kind"] for w in weapons if w is not None}
        has_lightning = "lightning" in held_kinds
        has_laser = "laser" in held_kinds
        has_bis = has_lightning and ("gun" in held_kinds)
        empty_slot = 0 if weapons[0] is None else (1 if weapons[1] is None else None)

        for p in obs.get("pickups", []):
            if p["kind"] in held_kinds:
                continue
            if AVOID_MORTAR and p["kind"] == "mortar":
                continue
            if dist2d(pos, p["pos"]) <= self.pickup_r - 0.15 and abs(pos[2] - p["pos"][2]) <= 3.8:
                if empty_slot is not None:
                    action["take"] = p["id"]
                    break
                worst_slot = None
                worst_rank = self.weapon_rank.get(p["kind"], 0)
                for s_idx, w in enumerate(weapons):
                    if w is None:
                        continue
                    if w["kind"] == "laser" and w.get("charge", 0) > 0:
                        continue
                    r = self.weapon_rank.get(w["kind"], 0)
                    if r < worst_rank:
                        worst_rank = r
                        worst_slot = s_idx
                if worst_slot is not None:
                    action["take"] = p["id"]
                    action["drop"] = worst_slot
                    break

        # 3. Classify entities
        enemies = [m for m in obs.get("mechs", []) if m["kind"] == "mech"]
        npcs = [m for m in obs.get("mechs", []) if m["kind"] == "npc"]
        crates = obs.get("crates", [])
        cubes = obs.get("cubes", [])
        pickups = obs.get("pickups", [])
        drops = obs.get("drops", [])
        zone = obs["zone"]
        zc = zone["next_center"]
        zr = zone["next_radius"]

        enemies.sort(key=lambda e: dist2d(pos, e["pos"]))
        near_enemies = [e for e in enemies if dist2d(pos, e["pos"]) < 85.0]
        nearest_d = dist2d(pos, enemies[0]["pos"]) if enemies else 1e9

        # 4. Target selection
        target = None
        target_type = None
        target_los = False

        if enemies:
            scored = []
            for e in enemies[:3]:
                ep = e["pos"]
                d = dist2d(pos, ep)
                los = self.map.has_los(muzzle_pos, (ep[0], ep[1], ep[2] + 4.5))
                hp = e.get("hp", 1000.0)
                hp_w = 0.42 + 0.58 * (hp / max(e.get("max_hp", 1400.0), 1.0))
                e_yaw = e.get("aim", [0.0, 0.0])[0]
                back_err = abs(angle_diff(math.atan2(pos[1] - ep[1], pos[0] - ep[0]), e_yaw))
                backstab_bonus = -30.0 if (los and back_err > 1.45) else 0.0
                exec_bonus = -32.0 if (los and hp < 350.0) else 0.0
                scored.append((d * hp_w + backstab_bonus + exec_bonus + (0.0 if los else 130.0), los, e))
            scored.sort(key=lambda x: x[0])
            _, target_los, target = scored[0]
            target_type = "mech"

        # Farm NPCs/crates for cubes (fixed: both considered, nearest wins)
        if my_cubes < SNOWBALL_CUBES and (target is None or nearest_d > FARM_MIN_ENEMY_DIST):
            cands = []
            if npcs:
                n = min(npcs, key=lambda x: dist2d(pos, x["pos"]))
                dn = dist2d(pos, n["pos"])
                if dn < 130.0:
                    cands.append((dn, n, "npc", 1.8))
            if crates:
                c = min(crates, key=lambda x: dist2d(pos, x["pos"]))
                dc = dist2d(pos, c["pos"])
                if dc < 110.0:
                    cands.append((dc, c, "crate", 1.2))
            if cands:
                cands.sort(key=lambda x: x[0])
                _, ft, ftype, zo = cands[0]
                f_los = self.map.has_los(muzzle_pos, (ft["pos"][0], ft["pos"][1], ft["pos"][2] + zo))
                if f_los or target is None:
                    target, target_type, target_los = ft, ftype, f_los

        # 4b. Situation assessment
        n_close = sum(1 for e in enemies if dist2d(pos, e["pos"]) < 80.0)
        n_seen = sum(1 for e in enemies if dist2d(pos, e["pos"]) < 130.0)
        recent_hit = (t - self.last_hit_t) < 1.5
        t_dist = dist2d(pos, target["pos"]) if target_type == "mech" else 0.0
        threats = [e for e in enemies if dist2d(pos, e["pos"]) < 170.0]

        # Heal / hide (regen 45 HP/s after 5 s without dealing or taking damage)
        exposed = False
        if HEAL_HIDE:
            if self.heal:
                if hp_frac >= HEAL_EXIT_FRAC or (t - self.heal_since) > 35.0:
                    self.heal = False
            else:
                finishing = (
                    target_type == "mech" and n_close <= 1
                    and target.get("hp", 1e9) < 250.0 and t_dist < 45.0 and hp_frac > 0.25
                )
                outgunned = (
                    target_type == "mech" and n_close <= 1 and t_dist < 100.0 and "hp" in target
                    and me["hp"] < 0.55 * target["hp"] and hp_frac < 0.80
                )
                if (not finishing) and (hp_frac < HEAL_ENTER_FRAC or outgunned) and (nearest_d < 120.0 or recent_hit):
                    self.heal = True
                    self.heal_since = t
            if self.heal:
                for e in enemies[:4]:
                    if self.enemy_sees_me(e, pos):
                        exposed = True
                        break

        # Retreat only when genuinely outnumbered; has an end (stand-and-fight valve)
        retreating = False
        if RETREAT_ENABLED and not self.heal:
            outnumbered = n_close >= 3 or n_seen >= 4 or (n_close >= 2 and hp_frac < 0.65)
            if t < self.stand_until:
                outnumbered = False
            if outnumbered:
                self.flee_until = t + 2.0
                if self.flee_since is None:
                    self.flee_since = t
            elif t >= self.flee_until:
                self.flee_since = None
            retreating = (t < self.flee_until) and bool(threats)
            if (
                retreating and self.flee_since is not None
                and (t - self.flee_since) > STAND_AFTER_S and nearest_d < 55.0 and hp_frac > 0.30
            ):
                self.stand_until = t + STAND_FOR_S
                self.flee_until = 0.0
                self.flee_since = None
                retreating = False

        # 5. Movement
        move_x, move_y = 0.0, 0.0
        want_boost = False
        pre_checked = False
        hold_still = False
        mode_label = "ROAM"
        peek_pt = None

        d_zone = dist2d(pos, zc)
        outside_next = d_zone > max(zr - 18.0, 5.0)
        urgent_zone = (not me["in_zone"]) or (d_zone > zr - 8.0 and t > zone["shrink_start"] - 12.0)
        busy = retreating or self.heal

        close_cube = None
        if cubes and not busy:
            c = min(cubes, key=lambda x: dist2d(pos, x["pos"]))
            if dist2d(pos, c["pos"]) < (28.0 if near_enemies else 115.0):
                close_cube = c

        best_drop = None
        if not busy or (self.heal and hp_frac < 0.5 and not near_enemies):
            for dr in drops:
                dd = dist2d(pos, dr["pos"])
                if dr["lands_in"] < 20.0 and dd < (185.0 if (hp_frac < 0.72 or my_cubes < 10) else 100.0):
                    if len(near_enemies) <= 1 or dd < 24.0:
                        best_drop = dr
                        break

        laser_w = next((w for w in weapons if w and w["kind"] == "laser"), None)
        laser_charge = laser_w.get("charge", 0.0) if laser_w else 0.0
        laser_full = laser_w["stats"].get("charge", 0.75) if laser_w else 0.75
        laser_ready = has_laser and laser_charge >= laser_full * 0.68

        if urgent_zone:
            mode_label = "ZONE"
            move_x, move_y = self.get_astar_dir(tick, pos, zc)
            want_boost = energy > 22.0
        elif self.heal:
            mode_label = "HEAL"
            if exposed or recent_hit or nearest_d < 45.0:
                th = threats if threats else enemies
                if th:
                    d_ev = self.map.evade_direction(pos, muzzle_h, th, zc, zr, self.prev_evade_dir)
                    if d_ev is None:
                        cx = sum(e["pos"][0] for e in th) / len(th)
                        cy = sum(e["pos"][1] for e in th) / len(th)
                        move_x, move_y = pos[0] - cx, pos[1] - cy
                    else:
                        move_x, move_y = d_ev
                        self.prev_evade_dir = d_ev
                        pre_checked = True
                elif self.last_damage_bearing is not None:
                    move_x = -math.cos(self.last_damage_bearing)
                    move_y = -math.sin(self.last_damage_bearing)
                want_boost = energy > 20.0 and (recent_hit or exposed)
            elif best_drop is not None:
                dd = dist2d(pos, best_drop["pos"])
                if DROP_HOLD and dd < 3.0:
                    hold_still = True
                else:
                    move_x, move_y = self.get_astar_dir(tick, pos, best_drop["pos"])
                    want_boost = energy > 35.0 and dd > best_drop["radius"]
            else:
                hold_still = True
        elif retreating:
            mode_label = "RETREAT"
            d_ev = self.map.evade_direction(pos, muzzle_h, threats, zc, zr, self.prev_evade_dir)
            if d_ev is None:
                cx = sum(e["pos"][0] for e in threats) / len(threats)
                cy = sum(e["pos"][1] for e in threats) / len(threats)
                move_x, move_y = pos[0] - cx, pos[1] - cy
            else:
                move_x, move_y = d_ev
                self.prev_evade_dir = d_ev
                pre_checked = True
            want_boost = energy > 20.0 and (took_hit or n_close >= 3)
        elif close_cube is not None:
            mode_label = "CUBE"
            move_x, move_y = self.get_astar_dir(tick, pos, close_cube["pos"])
            want_boost = energy > 38.0 and dist2d(pos, close_cube["pos"]) > 12.0
        elif best_drop is not None and (not near_enemies or hp_frac < 0.60):
            mode_label = "DROP"
            dd = dist2d(pos, best_drop["pos"])
            if not (DROP_HOLD and dd < 3.0):
                move_x, move_y = self.get_astar_dir(tick, pos, best_drop["pos"])
                want_boost = energy > 35.0 and dd > best_drop["radius"]
        elif target_type == "mech":
            ex, ey = target["pos"][0], target["pos"][1]
            dx, dy = ex - pos[0], ey - pos[1]
            d = max(math.hypot(dx, dy), 1e-4)
            nx, ny = dx / d, dy / d
            secondaries = [e for e in enemies[:4] if e is not target]

            if has_laser and not has_lightning and d > 34.0 and len(near_enemies) < 2:
                # Laser fallback: corner-peek (hide while charging, step out to fire)
                mode_label = "PEEK" if laser_ready else "HIDE_CHG"
                peek_pt = self.map.find_cover_and_peek(pos, muzzle_h, target, secondaries, laser_ready, zc, zr)
                if peek_pt is not None:
                    move_x, move_y = peek_pt[0] - pos[0], peek_pt[1] - pos[1]
                else:
                    sx, sy = -ny * self.strafe_dir, nx * self.strafe_dir
                    rad_b = -0.5 if d < KITE_IDEAL_DIST - 15.0 else (0.4 if d > KITE_IDEAL_DIST + 15.0 else 0.0)
                    move_x, move_y = nx * rad_b + sx * 0.85, ny * rad_b + sy * 0.85
            elif len(near_enemies) >= 2:
                mode_label = "ISOLATE"
                others = [e for e in near_enemies if e is not target]
                ox = sum(e["pos"][0] for e in others) / len(others)
                oy = sum(e["pos"][1] for e in others) / len(others)
                ax, ay = pos[0] - ox, pos[1] - oy
                am = max(math.hypot(ax, ay), 1e-4)
                ax, ay = ax / am, ay / am
                s1x, s1y = -ny, nx
                s2x, s2y = ny, -nx
                if (s1x * ax + s1y * ay) >= (s2x * ax + s2y * ay):
                    sx, sy = s1x, s1y
                else:
                    sx, sy = s2x, s2y
                zx, zy = zc[0] - pos[0], zc[1] - pos[1]
                zm = max(math.hypot(zx, zy), 1e-4)
                move_x = sx * 0.60 + ax * 0.55 + (zx / zm) * 0.15
                move_y = sy * 0.60 + ay * 0.55 + (zy / zm) * 0.15
                want_boost = energy > 30.0 and dist2d(pos, others[0]["pos"]) < 45.0
            else:
                mode_label = "BRAWL"
                e_yaw = target.get("aim", [0.0, 0.0])[0]
                rel = angle_diff(math.atan2(pos[1] - ey, pos[0] - ex), e_yaw)
                flank_dir = -1.0 if rel >= 0.0 else 1.0
                sx, sy = -ny * flank_dir, nx * flank_dir
                rush = has_lightning and hp_frac > 0.30
                ideal_dist = 27.0 if rush else 58.0
                if hp_frac < 0.26:
                    move_x = -nx * 0.72 + sx * 0.70
                    move_y = -ny * 0.72 + sy * 0.70
                    want_boost = energy > 24.0
                elif d > ideal_dist + 8.0:
                    move_x = nx * 0.57 + sx * 0.82
                    move_y = ny * 0.57 + sy * 0.82
                    want_boost = rush and d < 64.0 and energy > 36.0
                elif d < ideal_dist - 8.0:
                    move_x = -nx * 0.52 + sx * 0.85
                    move_y = -ny * 0.52 + sy * 0.85
                else:
                    move_x, move_y = sx, sy
                    if rush and d < 36.0 and 0.65 < abs(rel) < 1.90 and energy > 35.0:
                        want_boost = True

            if target.get("charging") and mode_label in ("BRAWL", "ISOLATE"):
                move_x, move_y = (-ny) * self.strafe_dir, nx * self.strafe_dir
                want_boost = energy > 15.0
        elif not has_bis and pickups and not near_enemies:
            lp = [p for p in pickups if p["kind"] == "lightning"]
            if lp and not has_lightning:
                valid_p = lp
            else:
                valid_p = [
                    p for p in pickups
                    if p["kind"] not in held_kinds and not (AVOID_MORTAR and p["kind"] == "mortar")
                ]
            if valid_p:
                mode_label = "LOOT"
                p = min(valid_p, key=lambda x: dist2d(pos, x["pos"]))
                move_x, move_y = self.get_astar_dir(tick, pos, p["pos"])
                want_boost = energy > 45.0
        elif target_type in ("npc", "crate"):
            mode_label = "FARM"
            if dist2d(pos, target["pos"]) > 28.0:
                move_x, move_y = self.get_astar_dir(tick, pos, target["pos"])
            else:
                dx, dy = target["pos"][0] - pos[0], target["pos"][1] - pos[1]
                move_x, move_y = -dy * self.strafe_dir, dx * self.strafe_dir
        elif outside_next:
            mode_label = "ROTATE"
            move_x, move_y = self.get_astar_dir(tick, pos, zc)
        else:
            if self.roam_target is None or dist2d(pos, self.roam_target) < 18.0:
                ang = self.rng.uniform(0.0, TWO_PI)
                rad = self.rng.uniform(0.15, 0.72) * max(zr - 15.0, 10.0)
                self.roam_target = (
                    clamp(zc[0] + math.cos(ang) * rad, self.map.bounds[0] + 10.0, self.map.bounds[1] - 10.0),
                    clamp(zc[1] + math.sin(ang) * rad, self.map.bounds[2] + 10.0, self.map.bounds[3] - 10.0),
                )
            move_x, move_y = self.get_astar_dir(tick, pos, self.roam_target)

        if LASER_DODGE:
            ld = self.laser_dodge(pos, enemies)
            if ld is not None:
                move_x, move_y = ld
                pre_checked = False
                hold_still = False
                if energy > 25.0:
                    want_boost = True

        if pre_checked:
            mx, my = move_x, move_y
        else:
            mx, my = self.map.steer_around_walls(pos, move_x, move_y)
        action["move"] = [round(mx, 4), round(my, 4)]
        action["boost"] = want_boost

        if self.stuck_ticks >= 4 and not hold_still:
            if me["grounded"]:
                action["jump"] = True
            elif energy > 25.0:
                action["boost"] = True
            if self.stuck_ticks >= 10:
                self.strafe_dir = -self.strafe_dir
                self.roam_target = None
                self.path_wp = None
                self.prev_evade_dir = None
                self.stuck_ticks = 0

        # 6. Aim & fire (keeps shooting while retreating: free damage)
        if target is not None:
            tpos = target["pos"]
            tvel = target.get("vel", [0.0, 0.0, 0.0])
            z_off = 4.5 if target_type == "mech" else (1.8 if target_type == "npc" else 1.2)
            aim_center = (tpos[0], tpos[1], tpos[2] + z_off)
            d_target = dist3d(muzzle_pos, aim_center)

            gun_speed = 252.0
            for w in weapons:
                if w and w["kind"] == "gun":
                    gun_speed = w["stats"].get("speed", 252.0)

            lead_pt = self.lead_target(muzzle_pos, aim_center, tvel, gun_speed) if target_type != "crate" else aim_center
            des_yaw, des_pitch = self.aim_at_point(muzzle_pos, lead_pt)
            yaw_err = abs(angle_diff(des_yaw, cur_yaw))

            fire_flags = [False, False]
            for s_idx, w in enumerate(weapons):
                if w is None:
                    continue
                kind = w["kind"]
                wstats = w.get("stats", {})

                if kind == "lightning":
                    finishing_k = target_type == "mech" and target.get("hp", 1000.0) < 150.0
                    min_energy = 5.0 if (finishing_k or not retreating) else 22.0
                    if target_los and d_target <= wstats.get("range", 45.0) - 1.0 and yaw_err < 0.48 and energy > min_energy:
                        fire_flags[s_idx] = True

                elif kind == "gun":
                    if target_los and d_target <= wstats.get("range", 230.0) and yaw_err < 0.15 and w.get("reload", 0.0) <= 0.0:
                        fire_flags[s_idx] = True

                elif kind == "laser":
                    charge = w.get("charge", 0.0)
                    full_charge = wstats.get("charge", 0.75)
                    min_charge = wstats.get("min_charge", 0.15)
                    if target_type == "mech" and d_target <= wstats.get("range", 280.0):
                        lyaw, lpitch = self.aim_at_point(muzzle_pos, aim_center)
                        lyaw_err = abs(angle_diff(lyaw, cur_yaw))
                        calm = not (retreating or self.heal) and nearest_d > 40.0
                        if charge == 0.0:
                            # charge (slows us to half speed) only when calm; hidden charging is allowed
                            if calm and w.get("cooldown", 0.0) <= 0.0 and d_target < 230.0:
                                fire_flags[s_idx] = True
                        else:
                            if target_los and (
                                (charge >= full_charge * 0.75 and lyaw_err < 0.07)
                                or (charge >= min_charge and target.get("hp", 1000.0) < 170.0 and lyaw_err < 0.08)
                            ):
                                des_yaw, des_pitch = lyaw, lpitch
                                fire_flags[s_idx] = False      # release = fire
                            elif not calm or charge >= full_charge * 1.6:
                                fire_flags[s_idx] = False      # release early: need full speed / avoid auto-fire
                            else:
                                if target_los:
                                    des_yaw, des_pitch = lyaw, lpitch
                                fire_flags[s_idx] = True

            if hold_still:
                fire_flags = [False, False]
                for w in weapons:
                    if w and w["kind"] == "gun" and w.get("reload", 0.0) <= 0.0:
                        if w.get("ammo", 12) < w["stats"].get("mag", 12):
                            action["reload"] = True
            elif not target_los:
                for w in weapons:
                    if w and w["kind"] == "gun" and w.get("reload", 0.0) <= 0.0:
                        if w.get("ammo", 12) < w["stats"].get("mag", 12) * 0.55:
                            action["reload"] = True

            action["aim"] = [round(des_yaw, 4), round(des_pitch, 4)]
            action["fire"] = fire_flags
        else:
            if self.last_damage_bearing is not None and (t - self.last_damage_t) < 2.5:
                action["aim"] = [round(self.last_damage_bearing, 4), 0.0]
            elif hold_still:
                action["aim"] = [cur_yaw, 0.0]
            elif obs.get("sounds"):
                near_sounds = [s for s in obs["sounds"] if s["dist"] in ("near", "mid")]
                if near_sounds:
                    action["aim"] = [round(near_sounds[0]["bearing"], 4), 0.0]
            else:
                action["aim"] = [round(wrap_angle(cur_yaw + 0.35), 4), 0.0]

            for w in weapons:
                if w and w["kind"] == "gun" and w.get("reload", 0.0) <= 0.0:
                    mag = w["stats"].get("mag", 12)
                    thresh = 1.0 if hold_still else 0.65
                    if w.get("ammo", mag) < mag * thresh:
                        action["reload"] = True

        # 6b. Debug overlay (team-only)
        if DEBUG_DRAW:
            try:
                mf.draw.label(
                    f"{mode_label}{' HOLD' if hold_still else ''} hp{int(hp_frac * 100)} "
                    f"c{n_close} s{n_seen} e{int(energy)} cu{my_cubes} a{obs.get('alive')}"
                )
                for e in enemies[:4]:
                    ep = e["pos"]
                    mf.draw.line(
                        [pos[0], pos[1], pos[2] + 7.0], [ep[0], ep[1], ep[2] + 4.5],
                        "#ff4040" if self.enemy_sees_me(e, pos) else "#808080",
                    )
                if peek_pt is not None:
                    mf.draw.point([peek_pt[0], peek_pt[1], pos[2] + 2.0], "#ffe040", 6)
                elif self.path_wp is not None:
                    mf.draw.point([self.path_wp[0], self.path_wp[1], pos[2] + 2.0], "#40ff60", 5)
            except Exception:
                pass

        # 7. Logging
        if LOGGING and (took_hit or n_close > 0 or self.heal) and t - self._last_log > 1.0:
            self._last_log = t
            try:
                mf.log(
                    f"t={t:.0f} mode={mode_label} alive={obs.get('alive')} hp={hp_frac:.2f} close={n_close} "
                    f"seen={n_seen} en={energy:.0f} inzone={int(me['in_zone'])} dz={d_zone:.0f}/{zr:.0f} cubes={my_cubes}"
                )
            except Exception:
                pass

        return action


if __name__ == "__main__":
    mf.run(Bot)