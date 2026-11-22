#!/usr/bin/env python3
"""
ApexMech v28.0 - The Architect (mechfight/1)
Complete architectural rebuild combining 4 high-elo pillars:
1. Precomputed 18m Grid + Capped A* Pathfinding (routes cleanly around buildings)
2. Early-Game Cube-Vacuum Snowball (stacks stats before taking fair 1v1s)
3. Laser + Gun Corner-Peeking (charges behind cover, peeks to fire, ducks back)
4. Third-Party Vulture Mode (waits on 2+ brawling bots, snipes low-HP kills)
Standard library only. (<4ms/tick)
"""
import heapq
import json
import math
import os
import random
import mechfight as mf

# ---------------- ARCHITECTURE TUNING ----------------
SNOWBALL_TIME_S = 70.0      # prioritize cubes/NPCs/crates/drops early
SNOWBALL_CUBES = 10         # exit early farm mode once we hit this many cubes
THIRD_PARTY_DIST = 55.0     # two enemies within 55m of each other = brawling
VULTURE_EXEC_HP = 480.0     # snipe/commit threshold during a third-party
LASER_DODGE = True
AVOID_MORTAR = True
DROP_HOLD = True
LOGGING = True
DEBUG_DRAW = True
# -----------------------------------------------------

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
    """Spatial-grid collision + LOS + 18m A* navigation grid + Corner-Peek finder."""

    def __init__(self, map_dir: str):
        self.cell_size = 45.0
        self.grid: dict[tuple[int, int], list[int]] = {}
        self.prisms: list[tuple] = []
        self.bounds = (-508.0, 551.0, -329.0, 384.0)

        # Navigation grid for A*
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
                        minx,
                        miny,
                        maxx,
                        maxy,
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
        """One-time startup precomputation of ground-level blocked cells for A*."""
        minx, maxx, miny, maxy = self.bounds
        step = self.nav_step
        gx0 = int(math.floor(minx / step))
        gx1 = int(math.floor(maxx / step))
        gy0 = int(math.floor(miny / step))
        gy1 = int(math.floor(maxy / step))
        for gx in range(gx0, gx1 + 1):
            wx = (gx + 0.5) * step
            for gy in range(gy0, gy1 + 1):
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
        steps = min(int(d / 11.0), max_steps)
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
        """Fast capped A* on the 18m grid; returns the next world (x, y) waypoint."""
        step = self.nav_step
        sx, sy = int(math.floor(pos[0] / step)), int(math.floor(pos[1] / step))
        gx, gy = int(math.floor(goal_xy[0] / step)), int(math.floor(goal_xy[1] / step))
        if (sx, sy) == (gx, gy):
            return goal_xy

        # If goal cell is slightly inside a building edge, still allow reaching it
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

        # Reconstruct path and pick the 2nd step ahead for smooth steering
        node = best_node
        path = []
        while node in came_from:
            path.append(node)
            node = came_from[node]
        if not path:
            return goal_xy
        path.reverse()
        pick = path[min(1, len(path) - 1)]
        return ((pick[0] + 0.5) * step, (pick[1] + 0.5) * step)

    def find_cover_and_peek(self, pos, muzzle_h: float, target, secondaries, want_peek: bool, zc, zr: float):
        """
        Corner-Peeking Engine:
        Samples 12 directions at 10m and 18m around `pos`.
        - If `want_peek` is True (Laser charged / ready to shoot): picks a spot with LOS to `target`
          while minimizing LOS from `secondaries`.
        - If `want_peek` is False (Laser charging or on cooldown): picks a Hidden spot with NO LOS
          to `target` (or `secondaries`) that stays close to the corner so we can peek next tick.
        """
        x, y, z = pos[0], pos[1], pos[2]
        tp = target["pos"]
        t_aim = (tp[0], tp[1], tp[2] + 4.5)
        zx, zy = zc[0], zc[1]
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
                score = 0.0

                if want_peek:
                    if sees_t:
                        score += 90.0
                    else:
                        score -= 70.0
                else:
                    # Charging behind cover: strongly prefer NO LOS to target
                    if not sees_t:
                        score += 95.0
                    else:
                        score -= 80.0

                # Prefer closer step (10m) so corner-peeking is snappy
                score -= rad * 1.1

                # Penalize exposure to secondary enemies
                for se in secondaries[:3]:
                    sp = se["pos"]
                    sd = math.hypot(nx - sp[0], ny - sp[1])
                    if sd < 150.0:
                        if self.has_los(cm, (sp[0], sp[1], sp[2] + 4.5), max_steps=6):
                            score -= 75.0 + max(0.0, 65.0 - sd) * 0.6
                        else:
                            score += 25.0

                # Zone safety
                dz = math.hypot(nx - zx, ny - zy)
                if dz > safe_r:
                    score -= (dz - safe_r) * 3.0

                if score > best_score:
                    best_score = score
                    best_pt = (nx, ny)

        return best_pt

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


class Bot:
    name = "ApexMech-v28.0"

    def __init__(self, hello: mf.Hello):
        self.slot = hello["slot"]
        self.rng = random.Random(hello["seed"])
        self.config = hello["config"]
        self.map = ArenaMap(hello["map"]["dir"])
        self.pickup_r = self.config.get("loot", {}).get("pickup_radius", 4.0)

        # Corner-Peeker + Third-Party Meta: Laser + Gun primary, Lightning backup
        self.weapon_rank = {"laser": 5, "gun": 4, "lightning": 3, "mortar": 1}
        self.upgrade_weights = {
            "armor": 100,
            "laser": 99,
            "hp": 97,
            "gun_rate": 96,
            "gun_damage": 95,
            "speed": 94,
            "regen": 92,
            "reload": 90,
            "boost": 88,
            "energy": 87,
            "lightning": 85,
            "sensors": 70,
            "mortar": 25,
        }

        self.last_pos = None
        self.stuck_ticks = 0
        self.strafe_dir = 1.0
        self.strafe_timer = 0
        self.strafe_period = 7
        self.last_damage_bearing = None
        self.last_damage_t = -10.0
        self.roam_target = None
        self.locked_id = None

        # A* cache
        self.path_wp = None
        self.path_goal = None
        self.path_tick = -100
        self._last_log = -9.0

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

    def solve_intercept(self, muzzle_pos, target_pos, target_vel, proj_speed: float):
        """Exact 3D quadratic intercept solver for Gun projectiles."""
        rx = target_pos[0] - muzzle_pos[0]
        ry = target_pos[1] - muzzle_pos[1]
        rz = target_pos[2] - muzzle_pos[2]
        vx, vy, vz = target_vel[0], target_vel[1], target_vel[2]

        a = vx * vx + vy * vy + vz * vz - proj_speed * proj_speed
        b = 2.0 * (rx * vx + ry * vy + rz * vz)
        c = rx * rx + ry * ry + rz * rz

        t_hit = 0.0
        if abs(a) < 1e-6:
            if abs(b) > 1e-6:
                t_hit = max(0.0, -c / b)
        else:
            disc = b * b - 4.0 * a * c
            if disc >= 0.0:
                sqrt_d = math.sqrt(disc)
                t1 = (-b - sqrt_d) / (2.0 * a)
                t2 = (-b + sqrt_d) / (2.0 * a)
                if t1 > 0.0 and t2 > 0.0:
                    t_hit = min(t1, t2)
                elif t1 > 0.0:
                    t_hit = t1
                elif t2 > 0.0:
                    t_hit = t2

        t_hit = clamp(t_hit, 0.0, 1.15)
        return (
            target_pos[0] + vx * t_hit,
            target_pos[1] + vy * t_hit,
            target_pos[2] + vz * t_hit,
        )

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
        """Uses cached A* waypoint (replans every 10 ticks or if goal shifts >22m)."""
        if (
            self.path_wp is None
            or self.path_goal is None
            or (tick - self.path_tick) >= 10
            or dist2d(goal_xy, self.path_goal) > 22.0
            or dist2d(pos, self.path_wp) < 8.0
        ):
            self.path_wp = self.map.find_path_waypoint(pos, goal_xy, max_nodes=140)
            self.path_goal = goal_xy
            self.path_tick = tick
        return self.path_wp[0] - pos[0], self.path_wp[1] - pos[1]

    def detect_third_party(self, enemies):
        """Returns (brawler_a, brawler_b) if two visible enemies are actively fighting each other."""
        n = min(len(enemies), 5)
        for i in range(n):
            for j in range(i + 1, n):
                if dist2d(enemies[i]["pos"], enemies[j]["pos"]) < THIRD_PARTY_DIST:
                    return (enemies[i], enemies[j])
        return None

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

        # 2. Weapon pickup / swap (Hard-lock Laser + Gun)
        held_kinds = {w["kind"] for w in weapons if w is not None}
        has_laser = "laser" in held_kinds
        has_lightning = "lightning" in held_kinds
        has_bis = has_laser and ("gun" in held_kinds)
        empty_slot = 0 if weapons[0] is None else (1 if weapons[1] is None else None)

        if not has_bis:
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
        near_enemies = [e for e in enemies if dist2d(pos, e["pos"]) < 90.0]

        # Detect active 3rd-party brawl
        brawl = self.detect_third_party(enemies)
        snowball_mode = (t < SNOWBALL_TIME_S) and (my_cubes < SNOWBALL_CUBES)

        # 4. Target Selection (Third-Party Vulture + Snowball Farm + Execute Lock)
        target = None
        target_type = None
        target_los = False

        if enemies:
            scored = []
            for e in enemies[:4]:
                ep = e["pos"]
                d = dist2d(pos, ep)
                los = self.map.has_los(muzzle_pos, (ep[0], ep[1], ep[2] + 4.5))
                hp = e.get("hp", 1000.0)
                hp_w = 0.38 + 0.62 * (hp / max(e.get("max_hp", 1400.0), 1.0))

                # Huge priority for kill-stealing low-HP brawlers
                vulture_bonus = -65.0 if (los and hp < VULTURE_EXEC_HP) else 0.0
                lock_bonus = -30.0 if (los and e["id"] == self.locked_id) else 0.0
                e_yaw = e.get("aim", [0.0, 0.0])[0]
                back_err = abs(angle_diff(math.atan2(pos[1] - ep[1], pos[0] - ep[0]), e_yaw))
                backstab_bonus = -25.0 if (los and back_err > 1.45) else 0.0

                scored.append((d * hp_w + vulture_bonus + lock_bonus + backstab_bonus + (0.0 if los else 135.0), los, e))
            scored.sort(key=lambda x: x[0])
            _, target_los, target = scored[0]
            target_type = "mech"
            self.locked_id = target["id"]
        else:
            self.locked_id = None

        # In Snowball Mode, prefer farming NPCs/crates unless a mech is close (<55m) or weak (<480 HP)
        mech_urgent = (
            target_type == "mech"
            and (dist2d(pos, target["pos"]) < 55.0 or target.get("hp", 1000.0) < VULTURE_EXEC_HP)
        )
        if (target is None or (snowball_mode and not mech_urgent)) and npcs:
            n = min(npcs, key=lambda x: dist2d(pos, x["pos"]))
            if dist2d(pos, n["pos"]) < 130.0:
                n_los = self.map.has_los(muzzle_pos, (n["pos"][0], n["pos"][1], n["pos"][2] + 1.8))
                target, target_type, target_los = n, "npc", n_los
        elif (target is None or (snowball_mode and not mech_urgent)) and crates:
            c = min(crates, key=lambda x: dist2d(pos, x["pos"]))
            if dist2d(pos, c["pos"]) < 110.0:
                c_los = self.map.has_los(muzzle_pos, (c["pos"][0], c["pos"][1], c["pos"][2] + 1.2))
                target, target_type, target_los = c, "crate", c_los

        # Check Laser charge state for Corner-Peeking
        laser_w = next((w for w in weapons if w and w["kind"] == "laser"), None)
        laser_charge = laser_w.get("charge", 0.0) if laser_w else 0.0
        laser_full = laser_w["stats"].get("charge", 0.75) if laser_w else 0.75
        laser_cd = laser_w.get("cooldown", 0.0) if laser_w else 0.0
        laser_ready_to_peek = has_laser and (laser_charge >= laser_full * 0.62)

        # 5. Movement: A* Snowball / Laser Corner-Peek / Third-Party Vulture / 1v1 Backstab
        move_x, move_y = 0.0, 0.0
        want_boost = False
        mode_label = "ROAM"
        peek_pt = None

        d_zone = dist2d(pos, zc)
        outside_next = d_zone > max(zr - 18.0, 5.0)
        urgent_zone = (not me["in_zone"]) or (d_zone > zr - 8.0 and t > zone["shrink_start"] - 12.0)

        close_cube = None
        if cubes:
            c = min(cubes, key=lambda x: dist2d(pos, x["pos"]))
            if dist2d(pos, c["pos"]) < (32.0 if near_enemies else 140.0):
                close_cube = c

        best_drop = None
        for dr in drops:
            dd = dist2d(pos, dr["pos"])
            if dr["lands_in"] < 22.0 and dd < (210.0 if (snowball_mode or hp_frac < 0.75) else 120.0):
                if len(near_enemies) <= 1 or dd < 25.0:
                    best_drop = dr
                    break

        if urgent_zone:
            mode_label = "ZONE"
            move_x, move_y = self.get_astar_dir(tick, pos, zc)
            want_boost = energy > 22.0
        elif close_cube is not None:
            mode_label = "CUBE"
            move_x, move_y = self.get_astar_dir(tick, pos, close_cube["pos"])
            want_boost = energy > 35.0 and dist2d(pos, close_cube["pos"]) > 14.0
        elif best_drop is not None and (not near_enemies or hp_frac < 0.60):
            mode_label = "DROP"
            dd = dist2d(pos, best_drop["pos"])
            if DROP_HOLD and dd < 3.0:
                pass
            else:
                move_x, move_y = self.get_astar_dir(tick, pos, best_drop["pos"])
                want_boost = energy > 35.0 and dd > best_drop["radius"]
        elif not has_bis and pickups and (not near_enemies or snowball_mode):
            # Prioritize finding Laser (or Gun if missing) via A*
            lp = [p for p in pickups if p["kind"] == "laser"]
            valid_p = lp if (lp and not has_laser) else [
                p for p in pickups if p["kind"] not in held_kinds and not (AVOID_MORTAR and p["kind"] == "mortar")
            ]
            if valid_p:
                mode_label = "LOOT"
                p = min(valid_p, key=lambda x: dist2d(pos, x["pos"]))
                move_x, move_y = self.get_astar_dir(tick, pos, p["pos"])
                want_boost = energy > 40.0
        elif target_type == "mech":
            ex, ey = target["pos"][0], target["pos"][1]
            dx, dy = ex - pos[0], ey - pos[1]
            d = max(math.hypot(dx, dy), 1e-4)
            nx, ny = dx / d, dy / d
            secondaries = [e for e in enemies[:4] if e is not target]

            if brawl is not None and d > 52.0 and target.get("hp", 1000.0) > VULTURE_EXEC_HP:
                # THIRD-PARTY VULTURE: Hold ~95m near cover while they kill each other
                mode_label = "VULTURE"
                want_peek = laser_ready_to_peek or (not has_laser)
                peek_pt = self.map.find_cover_and_peek(pos, muzzle_h, target, secondaries, want_peek, zc, zr)
                if peek_pt is not None:
                    move_x, move_y = peek_pt[0] - pos[0], peek_pt[1] - pos[1]
                elif d < 85.0:
                    move_x, move_y = -nx * 0.75 + (-ny * self.strafe_dir) * 0.65, -ny * 0.75 + (nx * self.strafe_dir) * 0.65
                else:
                    move_x, move_y = -ny * self.strafe_dir, nx * self.strafe_dir
            elif has_laser and d > 34.0:
                # LASER CORNER-PEEKER: Charge behind cover, peek out at >=62% charge to fire, duck back!
                mode_label = "PEEK" if laser_ready_to_peek else "HIDE_CHG"
                want_peek = laser_ready_to_peek or (laser_cd <= 0.0 and target.get("hp", 1000.0) < 280.0)
                peek_pt = self.map.find_cover_and_peek(pos, muzzle_h, target, secondaries, want_peek, zc, zr)
                if peek_pt is not None:
                    move_x, move_y = peek_pt[0] - pos[0], peek_pt[1] - pos[1]
                    if want_peek and not target_los and energy > 30.0:
                        want_boost = True
                else:
                    # Fallback mid-range strafe if in open courtyard
                    sx, sy = -ny * self.strafe_dir, nx * self.strafe_dir
                    ideal_d = 75.0
                    rad_b = 0.45 if d > ideal_d + 15.0 else (-0.45 if d < ideal_d - 15.0 else 0.0)
                    move_x, move_y = nx * rad_b + sx * 0.85, ny * rad_b + sy * 0.85
            else:
                # CLOSE-RANGE BRAWL / LIGHTNING BACKSTAB ORBIT (V18 core)
                mode_label = "BRAWL"
                if len(near_enemies) >= 2:
                    others = [e for e in near_enemies if e is not target]
                    ox = sum(e["pos"][0] for e in others) / len(others)
                    oy = sum(e["pos"][1] for e in others) / len(others)
                    ax, ay = pos[0] - ox, pos[1] - oy
                    am = max(math.hypot(ax, ay), 1e-4)
                    ax, ay = ax / am, ay / am
                    s1x, s1y = -ny, nx
                    s2x, s2y = ny, -nx
                    sx, sy = (s1x, s1y) if (s1x * ax + s1y * ay) >= (s2x * ax + s2y * ay) else (s2x, s2y)
                    move_x, move_y = sx * 0.60 + ax * 0.55, sy * 0.60 + ay * 0.55
                    want_boost = energy > 28.0 and dist2d(pos, others[0]["pos"]) < 45.0
                else:
                    e_yaw = target.get("aim", [0.0, 0.0])[0]
                    rel = angle_diff(math.atan2(pos[1] - ey, pos[0] - ex), e_yaw)
                    flank_dir = -1.0 if rel >= 0.0 else 1.0
                    sx, sy = -ny * flank_dir, nx * flank_dir
                    ideal_dist = 27.0 if (has_lightning and hp_frac > 0.30) else 56.0
                    if hp_frac < 0.28:
                        move_x, move_y = -nx * 0.72 + sx * 0.70, -ny * 0.72 + sy * 0.70
                        want_boost = energy > 22.0
                    elif d > ideal_dist + 8.0:
                        move_x, move_y = nx * 0.57 + sx * 0.82, ny * 0.57 + sy * 0.82
                        want_boost = has_lightning and d < 62.0 and energy > 35.0
                    elif d < ideal_dist - 8.0:
                        move_x, move_y = -nx * 0.52 + sx * 0.85, -ny * 0.52 + sy * 0.85
                    else:
                        move_x, move_y = sx, sy
                        if has_lightning and d < 36.0 and 0.65 < abs(rel) < 1.90 and energy > 35.0:
                            want_boost = True
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
                rad = self.rng.uniform(0.20, 0.75) * max(zr - 18.0, 12.0)
                self.roam_target = (
                    clamp(zc[0] + math.cos(ang) * rad, self.map.bounds[0] + 10.0, self.map.bounds[1] - 10.0),
                    clamp(zc[1] + math.sin(ang) * rad, self.map.bounds[2] + 10.0, self.map.bounds[3] - 10.0),
                )
            move_x, move_y = self.get_astar_dir(tick, pos, self.roam_target)

        # Laser dodge overrides movement
        if LASER_DODGE:
            ld = self.laser_dodge(pos, enemies)
            if ld is not None:
                move_x, move_y = ld
                if energy > 20.0:
                    want_boost = True

        mx, my = self.map.steer_around_walls(pos, move_x, move_y)
        action["move"] = [round(mx, 4), round(my, 4)]
        action["boost"] = want_boost

        if self.stuck_ticks >= 4:
            if me["grounded"]:
                action["jump"] = True
            elif energy > 25.0:
                action["boost"] = True
            if self.stuck_ticks >= 10:
                self.strafe_dir = -self.strafe_dir
                self.roam_target = None
                self.path_wp = None
                self.stuck_ticks = 0

        # 6. Aimbot & Firing (Pre-charge Laser behind cover, release on peek!)
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

            lead_pt = self.solve_intercept(muzzle_pos, aim_center, tvel, gun_speed) if target_type != "crate" else aim_center
            des_yaw, des_pitch = self.aim_at_point(muzzle_pos, lead_pt)
            yaw_err = abs(angle_diff(des_yaw, cur_yaw))

            fire_flags = [False, False]
            for s_idx, w in enumerate(weapons):
                if w is None:
                    continue
                kind = w["kind"]
                wstats = w.get("stats", {})

                if kind == "laser":
                    charge = w.get("charge", 0.0)
                    full_charge = wstats.get("charge", 0.75)
                    min_charge = wstats.get("min_charge", 0.15)
                    if target_type == "mech" and d_target <= wstats.get("range", 280.0):
                        # Hitscan aim directly at center mass
                        lyaw, lpitch = self.aim_at_point(muzzle_pos, aim_center)
                        lyaw_err = abs(angle_diff(lyaw, cur_yaw))
                        if charge == 0.0:
                            # Start charging EVEN BEHIND COVER so we peek at full charge!
                            if w.get("cooldown", 0.0) <= 0.0 and d_target < 220.0:
                                fire_flags[s_idx] = True
                        else:
                            # Release to fire ONLY when we have LOS and tight aim
                            if target_los and (
                                (charge >= full_charge * 0.75 and lyaw_err < 0.07)
                                or (charge >= min_charge and target.get("hp", 1000.0) < 170.0 and lyaw_err < 0.08)
                            ):
                                des_yaw, des_pitch = lyaw, lpitch
                                fire_flags[s_idx] = False
                            else:
                                # Keep holding charge while stepping out of cover
                                if target_los:
                                    des_yaw, des_pitch = lyaw, lpitch
                                fire_flags[s_idx] = True

                elif kind == "lightning":
                    if target_los and d_target <= wstats.get("range", 45.0) - 1.0 and yaw_err < 0.48 and energy > 5.0:
                        fire_flags[s_idx] = True

                elif kind == "gun":
                    if target_los and d_target <= wstats.get("range", 230.0) and yaw_err < 0.15 and w.get("reload", 0.0) <= 0.0:
                        fire_flags[s_idx] = True

            if not target_los:
                for w in weapons:
                    if w and w["kind"] == "gun" and w.get("reload", 0.0) <= 0.0:
                        if w.get("ammo", 12) < w["stats"].get("mag", 12) * 0.60:
                            action["reload"] = True

            action["aim"] = [round(des_yaw, 4), round(des_pitch, 4)]
            action["fire"] = fire_flags
        else:
            if self.last_damage_bearing is not None and (t - self.last_damage_t) < 2.5:
                action["aim"] = [round(self.last_damage_bearing, 4), 0.0]
            elif obs.get("sounds"):
                near_sounds = [s for s in obs["sounds"] if s["dist"] in ("near", "mid")]
                if near_sounds:
                    action["aim"] = [round(near_sounds[0]["bearing"], 4), 0.0]
            else:
                action["aim"] = [round(wrap_angle(cur_yaw + 0.35), 4), 0.0]

            for w in weapons:
                if w and w["kind"] == "gun" and w.get("reload", 0.0) <= 0.0:
                    mag = w["stats"].get("mag", 12)
                    if w.get("ammo", mag) < mag * 0.70:
                        action["reload"] = True

        # 6b. Debug overlay
        if DEBUG_DRAW:
            try:
                mf.draw.label(f"{mode_label} hp{int(hp_frac * 100)} c{my_cubes} e{int(energy)} a{obs.get('alive')}")
                if peek_pt is not None:
                    mf.draw.point([peek_pt[0], peek_pt[1], pos[2] + 2.0], "#ffe040", 6)
                elif self.path_wp is not None:
                    mf.draw.point([self.path_wp[0], self.path_wp[1], pos[2] + 2.0], "#40ff60", 5)
            except Exception:
                pass

        # 7. Logging
        if LOGGING and (took_hit or near_enemies) and t - self._last_log > 1.0:
            self._last_log = t
            try:
                mf.log(
                    f"t={t:.0f} mode={mode_label} alive={obs.get('alive')} hp={hp_frac:.2f} "
                    f"cubes={my_cubes} near={len(near_enemies)} en={energy:.0f} dz={d_zone:.0f}/{zr:.0f}"
                )
            except Exception:
                pass

        return action


if __name__ == "__main__":
    mf.run(Bot)