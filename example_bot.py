#!/usr/bin/env python3
"""
ApexMech v20.0 - Thanos Pie-Slicer / No-Hit Isolator (mechfight/1)
- 8-direction building-cover pie-slicing: puts buildings between you & secondary enemies
  while keeping LOS on your locked target
- Hard kill-lock: finishes targets to delete DPS & chain cube heals (zero hold-fire)
- 1v1 full-speed 1.5x rear-armor backstab orbit + exact 3D quadratic aimbot
Standard library only. (<3ms/tick)
"""
import json
import math
import os
import random
import mechfight as mf


TWO_PI = 6.283185307179586
DIRS_8 = tuple((math.cos(i * 0.7853981633974483), math.sin(i * 0.7853981633974483)) for i in range(8))


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
    def __init__(self, map_dir: str):
        self.cell_size = 45.0
        self.grid: dict[tuple[int, int], list[int]] = {}
        self.prisms: list[tuple] = []
        self.bounds = (-508.0, 551.0, -329.0, 384.0)

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
            except Exception as e:
                mf.log(f"[map] load error: {e}")

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

    def pie_slice_direction(self, pos, muzzle_h: float, primary, secondaries, ideal_dist: float, zc, zr: float):
        """
        NO-HIT PIE-SLICER:
        Evaluates 8 candidate positions (12m away) and picks the move that:
        1. Uses buildings to BLOCK LOS from secondary enemies (0 incoming crossfire!)
        2. Keeps LOS on our locked primary target so we finish the kill
        3. Stays near ideal_dist & inside the safe zone
        """
        x, y, z = pos[0], pos[1], pos[2]
        px, py, pz = primary["pos"][0], primary["pos"][1], primary["pos"][2] + 4.5
        zx, zy = zc[0], zc[1]
        safe_r = max(zr - 10.0, 8.0)

        best_score = -1e9
        best_dir = None

        for ca, sa in DIRS_8:
            # Must not walk into a wall
            if self.is_blocked_body(x + ca * 5.0, y + sa * 5.0, z):
                continue
            nx, ny = x + ca * 12.0, y + sa * 12.0
            if self.is_blocked_body(nx, ny, z):
                continue

            cand_muzzle = (nx, ny, z + muzzle_h)
            score = 0.0

            # 1. MASSIVE PENALTY for any secondary enemy that can shoot us at this spot
            for se in secondaries[:3]:
                sp = se["pos"]
                sd = math.hypot(nx - sp[0], ny - sp[1])
                if sd < 150.0:
                    if self.has_los(cand_muzzle, (sp[0], sp[1], sp[2] + 4.5), max_steps=7):
                        score -= 95.0 + max(0.0, 70.0 - sd) * 0.8
                    else:
                        # Hidden behind a building from this enemy!
                        score += 40.0

            # 2. Keep LOS on our locked primary target so we keep dealing 100% DPS
            pd = math.hypot(nx - px, ny - py)
            if self.has_los(cand_muzzle, (px, py, pz), max_steps=8):
                score += 65.0
                score -= abs(pd - ideal_dist) * 0.9
            else:
                score -= 20.0

            # 3. Zone safety
            dz = math.hypot(nx - zx, ny - zy)
            if dz > safe_r:
                score -= (dz - safe_r) * 3.0

            if score > best_score:
                best_score = score
                best_dir = (ca, sa)

        return best_dir


class Bot:
    name = "ApexMech-v20.0"

    def __init__(self, hello: mf.Hello):
        self.slot = hello["slot"]
        self.rng = random.Random(hello["seed"])
        self.config = hello["config"]
        self.map = ArenaMap(hello["map"]["dir"])
        self.pickup_r = self.config.get("loot", {}).get("pickup_radius", 4.0)

        # Best-in-slot: Gun + Lightning
        self.weapon_rank = {"lightning": 5, "gun": 4, "laser": 3, "mortar": 1}
        self.upgrade_weights = {
            "armor": 100,
            "lightning": 98,
            "hp": 97,
            "gun_rate": 95,
            "gun_damage": 94,
            "speed": 93,
            "regen": 90,
            "reload": 88,
            "boost": 86,
            "energy": 84,
            "laser": 78,
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
        self.roam_target = None
        self.locked_id = None

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
        """Exact 3D quadratic intercept aimbot for moving targets."""
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

        t_hit = clamp(t_hit, 0.0, 1.1)
        return (
            target_pos[0] + vx * t_hit,
            target_pos[1] + vy * t_hit,
            target_pos[2] + vz * t_hit,
        )

    def mortar_pitch(self, muzzle_pos, target_pos, speed: float, gravity: float) -> float:
        r = math.hypot(target_pos[0] - muzzle_pos[0], target_pos[1] - muzzle_pos[1])
        dz = target_pos[2] - muzzle_pos[2]
        if r < 1.0:
            return 0.7
        v2 = speed * speed
        disc = v2 * v2 - gravity * (gravity * r * r + 2.0 * dz * v2)
        if disc < 0:
            return 0.78
        return clamp(math.atan((v2 - math.sqrt(disc)) / (gravity * r)), -0.5, 1.3)

    def incoming_mortar_dodge(self, pos, projectiles):
        """Only dodges imminent mortars (which drain 45 energy + knockback)."""
        mx, my = pos[0], pos[1]
        for p in projectiles:
            if p.get("kind") != "mortar":
                continue
            px, py = p["pos"][0], p["pos"][1]
            vx, vy = p["vel"][0], p["vel"][1]
            spd2 = vx * vx + vy * vy
            if spd2 < 10.0:
                continue
            rx, ry = mx - px, my - py
            if rx * rx + ry * ry > 42.0 * 42.0:
                continue
            dot = rx * vx + ry * vy
            if dot <= 0.0:
                continue
            t_cpa = dot / spd2
            if t_cpa > 0.65:
                continue
            cpa_x = px + vx * t_cpa - mx
            cpa_y = py + vy * t_cpa - my
            if cpa_x * cpa_x + cpa_y * cpa_y < 56.0:
                spd = math.sqrt(spd2)
                return (-vy / spd) * self.strafe_dir, (vx / spd) * self.strafe_dir
        return None

    def act(self, obs: mf.Observation) -> mf.Action:
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
            "tick": obs["tick"],
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

        # 2. Weapon pickup / swap (Hard-lock Gun + Lightning)
        held_kinds = {w["kind"] for w in weapons if w is not None}
        has_lightning = "lightning" in held_kinds
        has_bis = has_lightning and ("gun" in held_kinds)
        empty_slot = 0 if weapons[0] is None else (1 if weapons[1] is None else None)

        if not has_bis:
            for p in obs.get("pickups", []):
                if p["kind"] in held_kinds:
                    continue
                if dist2d(pos, p["pos"]) <= self.pickup_r - 0.15 and abs(pos[2] - p["pos"][2]) <= 3.8:
                    if empty_slot is not None:
                        action["take"] = p["id"]
                        break
                    worst_slot = None
                    worst_rank = self.weapon_rank.get(p["kind"], 0)
                    for s_idx, w in enumerate(weapons):
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

        # 3. Classify visible entities
        enemies = [m for m in obs.get("mechs", []) if m["kind"] == "mech"]
        npcs = [m for m in obs.get("mechs", []) if m["kind"] == "npc"]
        crates = obs.get("crates", [])
        cubes = obs.get("cubes", [])
        pickups = obs.get("pickups", [])
        drops = obs.get("drops", [])
        zone = obs["zone"]
        zc = zone["next_center"]
        zr = zone["next_radius"]

        # 4. HARD KILL-LOCK Target Selection ("When we lock, we kill")
        enemies.sort(key=lambda e: dist2d(pos, e["pos"]))
        near_enemies = [e for e in enemies if dist2d(pos, e["pos"]) < 95.0]
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
                hp_w = 0.40 + 0.60 * (hp / max(e.get("max_hp", 1400.0), 1.0))

                # Huge lock-on bonus so we finish kills instead of splitting damage
                lock_bonus = -55.0 if (los and e["id"] == self.locked_id) else 0.0
                exec_bonus = -35.0 if (los and hp < 360.0) else 0.0

                e_yaw = e.get("aim", [0.0, 0.0])[0]
                back_err = abs(angle_diff(math.atan2(pos[1] - ep[1], pos[0] - ep[0]), e_yaw))
                backstab_bonus = -28.0 if (los and back_err > 1.45) else 0.0

                scored.append((d * hp_w + lock_bonus + exec_bonus + backstab_bonus + (0.0 if los else 135.0), los, e))
            scored.sort(key=lambda x: x[0])
            _, target_los, target = scored[0]
            target_type = "mech"
            self.locked_id = target["id"]
        else:
            self.locked_id = None

        # Farm NPCs/crates when no close enemy (<65m) and we want cubes/upgrades
        if (target is None or (my_cubes < 15 and dist2d(pos, target["pos"]) > 65.0)) and npcs:
            n = min(npcs, key=lambda x: dist2d(pos, x["pos"]))
            if dist2d(pos, n["pos"]) < 95.0:
                n_los = self.map.has_los(muzzle_pos, (n["pos"][0], n["pos"][1], n["pos"][2] + 1.8))
                if n_los or target is None:
                    target, target_type, target_los = n, "npc", n_los
        if (target is None or (my_cubes < 10 and target_type == "mech" and dist2d(pos, target["pos"]) > 75.0)) and crates:
            c = min(crates, key=lambda x: dist2d(pos, x["pos"]))
            if dist2d(pos, c["pos"]) < 70.0:
                c_los = self.map.has_los(muzzle_pos, (c["pos"][0], c["pos"][1], c["pos"][2] + 1.2))
                if c_los or target is None:
                    target, target_type, target_los = c, "crate", c_los

        # 5. Movement: Building-Cover Pie-Slicer (1v2+) & Blind-Spot Backstab (1v1)
        move_x, move_y = 0.0, 0.0
        want_boost = False
        used_pie_slice = False

        d_zone = dist2d(pos, zc)
        outside_next = d_zone > max(zr - 18.0, 5.0)
        urgent_zone = (not me["in_zone"]) or (d_zone > zr - 8.0 and t > zone["shrink_start"] - 12.0)

        close_cube = None
        if cubes:
            c = min(cubes, key=lambda x: dist2d(pos, x["pos"]))
            if dist2d(pos, c["pos"]) < (28.0 if near_enemies else 115.0):
                close_cube = c

        best_drop = None
        for dr in drops:
            dd = dist2d(pos, dr["pos"])
            if dr["lands_in"] < 20.0 and dd < (185.0 if (hp_frac < 0.72 or my_cubes < 10) else 100.0):
                if len(near_enemies) <= 1 or dd < 24.0:
                    best_drop = dr
                    break

        if urgent_zone:
            move_x, move_y = zc[0] - pos[0], zc[1] - pos[1]
            want_boost = energy > 22.0
        elif close_cube is not None:
            move_x, move_y = close_cube["pos"][0] - pos[0], close_cube["pos"][1] - pos[1]
            want_boost = energy > 38.0 and dist2d(pos, close_cube["pos"]) > 12.0
        elif best_drop is not None and (not near_enemies or hp_frac < 0.62):
            move_x, move_y = best_drop["pos"][0] - pos[0], best_drop["pos"][1] - pos[1]
            want_boost = energy > 35.0 and dist2d(pos, best_drop["pos"]) > best_drop["radius"]
        elif target_type == "mech":
            ex, ey = target["pos"][0], target["pos"][1]
            dx, dy = ex - pos[0], ey - pos[1]
            d = max(math.hypot(dx, dy), 1e-4)
            nx, ny = dx / d, dy / d

            if len(near_enemies) >= 2:
                # THANOS PIE-SLICER: Use buildings to block secondary enemies while shredding locked target!
                secondaries = [e for e in near_enemies if e is not target]
                ideal_d = 32.0 if (has_lightning and hp_frac > 0.34) else 56.0
                pie = self.map.pie_slice_direction(pos, muzzle_h, target, secondaries, ideal_d, zc, zr)
                if pie is not None:
                    move_x, move_y = pie
                    used_pie_slice = True
                    want_boost = energy > 28.0 and (took_hit or dist2d(pos, secondaries[0]["pos"]) < 42.0)
                else:
                    cx = sum(e["pos"][0] for e in near_enemies) / len(near_enemies)
                    cy = sum(e["pos"][1] for e in near_enemies) / len(near_enemies)
                    move_x, move_y = pos[0] - cx, pos[1] - cy
                    want_boost = energy > 28.0
            else:
                # 1v1 FULL-SPEED ASSASSIN BACKSTAB ORBIT
                e_yaw = target.get("aim", [0.0, 0.0])[0]
                rel = angle_diff(math.atan2(pos[1] - ey, pos[0] - ex), e_yaw)
                flank_dir = -1.0 if rel >= 0.0 else 1.0
                sx, sy = -ny * flank_dir, nx * flank_dir
                if self.map.is_blocked_body(pos[0] + sx * 6.0, pos[1] + sy * 6.0, pos[2]):
                    sx, sy = -sx, -sy

                ideal_dist = 27.0 if (has_lightning and hp_frac > 0.28) else 58.0
                if hp_frac < 0.24:
                    move_x = -nx * 0.72 + sx * 0.70
                    move_y = -ny * 0.72 + sy * 0.70
                    want_boost = energy > 24.0
                elif d > ideal_dist + 8.0:
                    move_x = nx * 0.60 + sx * 0.80
                    move_y = ny * 0.60 + sy * 0.80
                    want_boost = has_lightning and d < 66.0 and energy > 34.0
                elif d < ideal_dist - 7.0:
                    move_x = -nx * 0.50 + sx * 0.86
                    move_y = -ny * 0.50 + sy * 0.86
                else:
                    move_x, move_y = sx, sy
                    if has_lightning and d < 36.0 and 0.65 < abs(rel) < 1.90 and energy > 32.0:
                        want_boost = True

            if target.get("charging"):
                move_x, move_y = (-ny) * self.strafe_dir, nx * self.strafe_dir
                want_boost = energy > 15.0
                used_pie_slice = False
        elif not has_bis and pickups:
            lp = [p for p in pickups if p["kind"] == "lightning"]
            valid_p = lp if (lp and not has_lightning) else [p for p in pickups if p["kind"] not in held_kinds]
            if valid_p:
                p = min(valid_p, key=lambda x: dist2d(pos, x["pos"]))
                move_x, move_y = p["pos"][0] - pos[0], p["pos"][1] - pos[1]
                want_boost = energy > 45.0
        elif target_type in ("npc", "crate"):
            dx, dy = target["pos"][0] - pos[0], target["pos"][1] - pos[1]
            if math.hypot(dx, dy) > 26.0:
                move_x, move_y = dx, dy
            else:
                move_x, move_y = -dy * self.strafe_dir, dx * self.strafe_dir
        elif outside_next:
            move_x, move_y = zc[0] - pos[0], zc[1] - pos[1]
        else:
            if self.roam_target is None or dist2d(pos, self.roam_target) < 15.0:
                ang = self.rng.uniform(0.0, TWO_PI)
                rad = self.rng.uniform(0.15, 0.72) * max(zr - 15.0, 10.0)
                self.roam_target = (
                    clamp(zc[0] + math.cos(ang) * rad, self.map.bounds[0], self.map.bounds[1]),
                    clamp(zc[1] + math.sin(ang) * rad, self.map.bounds[2], self.map.bounds[3]),
                )
            move_x, move_y = self.roam_target[0] - pos[0], self.roam_target[1] - pos[1]

        # Dodge imminent mortar shells
        mdodge = self.incoming_mortar_dodge(pos, obs.get("projectiles", []))
        if mdodge is not None:
            move_x, move_y = mdodge
            used_pie_slice = False
            if energy > 20.0:
                want_boost = True

        if used_pie_slice:
            mx, my = move_x, move_y
        else:
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
                self.stuck_ticks = 0

        # 6. Aimbot & 100% Aggressive Firing (Zero Hold-Fire)
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

                if kind == "lightning":
                    if target_los and d_target <= wstats.get("range", 45.0) - 1.0 and yaw_err < 0.48 and energy > 5.0:
                        fire_flags[s_idx] = True

                elif kind == "gun":
                    if target_los and d_target <= wstats.get("range", 230.0) and yaw_err < 0.15 and w.get("reload", 0.0) <= 0.0:
                        fire_flags[s_idx] = True

                elif kind == "laser":
                    charge = w.get("charge", 0.0)
                    full_charge = wstats.get("charge", 0.75)
                    min_charge = wstats.get("min_charge", 0.15)
                    if target_type == "mech" and target_los and d_target <= wstats.get("range", 280.0):
                        des_yaw, des_pitch = self.aim_at_point(muzzle_pos, aim_center)
                        yaw_err = abs(angle_diff(des_yaw, cur_yaw))
                        if charge == 0.0:
                            if w.get("cooldown", 0.0) <= 0.0 and yaw_err < 0.45:
                                fire_flags[s_idx] = True
                        elif (charge >= full_charge * 0.72 and yaw_err < 0.08) or (
                            charge >= min_charge and target.get("hp", 1000) < 160 and yaw_err < 0.09
                        ):
                            fire_flags[s_idx] = False
                        else:
                            fire_flags[s_idx] = True

                elif kind == "mortar":
                    if d_target <= 215.0 and d_target >= 18.0 and w.get("cooldown", 0.0) <= 0.0:
                        des_pitch = self.mortar_pitch(muzzle_pos, aim_center, wstats.get("speed", 72.0), wstats.get("gravity", 22.0))
                        if yaw_err < 0.14:
                            fire_flags[s_idx] = True

            if not target_los:
                for w in weapons:
                    if w and w["kind"] == "gun" and w.get("reload", 0.0) <= 0.0:
                        if w.get("ammo", 12) < w["stats"].get("mag", 12) * 0.55:
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
                    if w.get("ammo", mag) < mag * 0.65:
                        action["reload"] = True

        return action


if __name__ == "__main__":
    mf.run(Bot)