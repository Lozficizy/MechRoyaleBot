#!/usr/bin/env python3
"""
ApexMech v1.5 - Mech Royale Bot (mechfight/1)
Uses official mechfight.py wrapper. Standard library only.
"""
import json
import math
import os
import random
import struct
import mechfight as mf


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def wrap_angle(a: float) -> float:
    while a > math.pi:
        a -= 2.0 * math.pi
    while a < -math.pi:
        a += 2.0 * math.pi
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
    """Spatial-grid lookup for prisms (buildings/stadium/bridges) and terrain."""

    def __init__(self, map_dir: str):
        self.cell_size = 40.0
        self.grid: dict[tuple[int, int], list[int]] = {}
        self.prisms: list[dict] = []
        self.bounds = (-512.0, 555.0, -333.0, 388.0)
        self.terrain = None
        self.nx = 0
        self.ny = 0
        self.min_x = 0.0
        self.min_y = 0.0
        self.step = 1.0

        world_path = os.path.join(map_dir, "v2", "world.json")
        terrain_path = os.path.join(map_dir, "v2", "terrain.i16")

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
                t = world.get("terrain", {})
                self.nx = int(t.get("nx", 0))
                self.ny = int(t.get("ny", 0))
                self.min_x = float(t.get("minX", 0.0))
                self.min_y = float(t.get("minY", 0.0))
                self.step = float(t.get("step", 1.0))

                for p in world.get("prisms", []):
                    ring = p.get("ring", [])
                    if len(ring) < 6:
                        continue
                    xs = ring[0::2]
                    ys = ring[1::2]
                    minx, maxx = min(xs), max(xs)
                    miny, maxy = min(ys), max(ys)
                    prism = {
                        "base": float(p.get("base", -100.0)),
                        "top": float(p.get("top", 100.0)),
                        "ring": ring,
                        "holes": p.get("holes", []),
                        "bbox": (minx, miny, maxx, maxy),
                    }
                    idx = len(self.prisms)
                    self.prisms.append(prism)
                    cx0 = int(math.floor(minx / self.cell_size))
                    cx1 = int(math.floor(maxx / self.cell_size))
                    cy0 = int(math.floor(miny / self.cell_size))
                    cy1 = int(math.floor(maxy / self.cell_size))
                    for cx in range(cx0, cx1 + 1):
                        for cy in range(cy0, cy1 + 1):
                            self.grid.setdefault((cx, cy), []).append(idx)
            except Exception as e:
                mf.log(f"[map] world.json load error: {e}")

        if os.path.exists(terrain_path) and self.nx > 0 and self.ny > 0:
            try:
                with open(terrain_path, "rb") as f:
                    raw = f.read()
                if len(raw) >= self.nx * self.ny * 2:
                    self.terrain = raw
            except Exception as e:
                mf.log(f"[map] terrain.i16 load error: {e}")

    def ground_z(self, x: float, y: float) -> float:
        if self.terrain is None:
            return 0.0
        i = clamp(int((x - self.min_x) / self.step), 0, self.nx - 1)
        j = clamp(int((y - self.min_y) / self.step), 0, self.ny - 1)
        return struct.unpack_from("<h", self.terrain, (j * self.nx + i) * 2)[0] * 0.01

    def point_in_prism(self, x: float, y: float, z: float) -> bool:
        cx = int(math.floor(x / self.cell_size))
        cy = int(math.floor(y / self.cell_size))
        cand = self.grid.get((cx, cy))
        if not cand:
            return False
        for idx in cand:
            p = self.prisms[idx]
            if z < p["base"] or z > p["top"]:
                continue
            minx, miny, maxx, maxy = p["bbox"]
            if x < minx or x > maxx or y < miny or y > maxy:
                continue
            if point_in_ring(x, y, p["ring"]):
                in_hole = False
                for h in p["holes"]:
                    if point_in_ring(x, y, h):
                        in_hole = True
                        break
                if not in_hole:
                    return True
        return False

    def is_blocked_body(self, x: float, y: float, z_feet: float, height: float = 9.0) -> bool:
        if x < self.bounds[0] or x > self.bounds[1] or y < self.bounds[2] or y > self.bounds[3]:
            return True
        cx = int(math.floor(x / self.cell_size))
        cy = int(math.floor(y / self.cell_size))
        cand = self.grid.get((cx, cy))
        if not cand:
            return False
        z_top = z_feet + height
        for idx in cand:
            p = self.prisms[idx]
            if z_top <= p["base"] or z_feet >= p["top"] - 1.2:
                continue
            minx, miny, maxx, maxy = p["bbox"]
            if x < minx or x > maxx or y < miny or y > maxy:
                continue
            if point_in_ring(x, y, p["ring"]):
                in_hole = False
                for h in p["holes"]:
                    if point_in_ring(x, y, h):
                        in_hole = True
                        break
                if not in_hole:
                    return True
        return False

    def has_los(self, a, b) -> bool:
        """Fast stepped 3D line-of-sight check against prisms."""
        d = dist3d(a, b)
        if d < 5.0:
            return True
        steps = min(int(d / 4.0), 45)
        if steps < 2:
            return True
        dx = (b[0] - a[0]) / steps
        dy = (b[1] - a[1]) / steps
        dz = (b[2] - a[2]) / steps
        x, y, z = a[0] + dx, a[1] + dy, a[2] + dz
        for _ in range(1, steps):
            if self.point_in_prism(x, y, z):
                return False
            x += dx
            y += dy
            z += dz
        return True

    def steer_around_walls(self, pos, desired_dx: float, desired_dy: float):
        mag = math.hypot(desired_dx, desired_dy)
        if mag < 1e-4:
            return 0.0, 0.0
        base_ang = math.atan2(desired_dy, desired_dx)
        x, y, z = pos[0], pos[1], pos[2]
        offsets = (0.0, 0.35, -0.35, 0.7, -0.7, 1.1, -1.1, 1.55, -1.55, 2.1, -2.1)
        for off in offsets:
            ang = base_ang + off
            ca, sa = math.cos(ang), math.sin(ang)
            if (
                not self.is_blocked_body(x + ca * 4.5, y + sa * 4.5, z)
                and not self.is_blocked_body(x + ca * 9.5, y + sa * 9.5, z)
            ):
                return ca, sa
        return desired_dx / mag, desired_dy / mag


class Bot:
    name = "ApexMech-v1.5"

    def __init__(self, hello: mf.Hello):
        self.slot = hello["slot"]
        self.rng = random.Random(hello["seed"])
        self.config = hello["config"]
        self.map = ArenaMap(hello["map"]["dir"])
        self.pickup_r = self.config.get("loot", {}).get("pickup_radius", 4.0)

        # Weapon preference
        self.weapon_rank = {"lightning": 4, "laser": 3, "gun": 2, "mortar": 1}

        # Upgrade preference keywords
        self.upgrade_weights = {
            "armor": 100,
            "hp": 96,
            "regen": 92,
            "lightning": 90,
            "gun_damage": 88,
            "gun_rate": 86,
            "laser": 84,
            "speed": 78,
            "boost": 76,
            "energy": 74,
            "sensors": 66,
            "reload": 62,
            "mortar": 45,
        }

        self.last_pos = None
        self.stuck_ticks = 0
        self.strafe_dir = 1.0
        self.strafe_timer = 0
        self.last_damage_bearing = None
        self.last_damage_t = -10.0
        self.roam_target = None

    def pick_upgrade(self, offer) -> int:
        best_i = 0
        best_score = -1e9
        for i, ch in enumerate(offer.get("choices", [])):
            uid = ch.get("id", "").lower()
            desc = ch.get("desc", "").lower()
            score = 50
            for k, w in self.upgrade_weights.items():
                if k in uid or k in desc:
                    score = max(score, w)
            if score > best_score:
                best_score = score
                best_i = i
        return best_i

    def aim_at_point(self, muzzle_pos, target_pos):
        dx = target_pos[0] - muzzle_pos[0]
        dy = target_pos[1] - muzzle_pos[1]
        dz = target_pos[2] - muzzle_pos[2]
        horiz = math.hypot(dx, dy)
        yaw = math.atan2(dy, dx)
        pitch = math.atan2(dz, max(horiz, 1e-4))
        return yaw, clamp(pitch, -1.2, 1.2)

    def lead_target(self, muzzle_pos, target_pos, target_vel, proj_speed: float):
        d = dist3d(muzzle_pos, target_pos)
        t_flight = d / max(proj_speed, 1.0)
        px = target_pos[0] + target_vel[0] * t_flight
        py = target_pos[1] + target_vel[1] * t_flight
        pz = target_pos[2] + target_vel[2] * t_flight
        d2 = dist3d(muzzle_pos, (px, py, pz))
        t_flight = d2 / max(proj_speed, 1.0)
        return (
            target_pos[0] + target_vel[0] * t_flight,
            target_pos[1] + target_vel[1] * t_flight,
            target_pos[2] + target_vel[2] * t_flight,
        )

    def mortar_pitch(self, muzzle_pos, target_pos, speed: float, gravity: float) -> float:
        dx = target_pos[0] - muzzle_pos[0]
        dy = target_pos[1] - muzzle_pos[1]
        dz = target_pos[2] - muzzle_pos[2]
        r = math.hypot(dx, dy)
        if r < 1.0:
            return 0.7
        v2 = speed * speed
        disc = v2 * v2 - gravity * (gravity * r * r + 2.0 * dz * v2)
        if disc < 0:
            return 0.78
        tan_theta = (v2 - math.sqrt(disc)) / (gravity * r)
        return clamp(math.atan(tan_theta), -0.5, 1.3)

    def act(self, obs: mf.Observation) -> mf.Action:
        t = obs["t"]
        me = obs["self"]
        pos = me["pos"]
        hp_frac = me["hp"] / max(me["max_hp"], 1.0)
        energy = me["energy"]
        weapons = me["weapons"]
        stats = me.get("stats", {})
        muzzle_h = stats.get("muzzle_height", 7.0)
        muzzle_pos = (pos[0], pos[1], pos[2] + muzzle_h)
        cur_yaw, _ = me["aim"]

        for ev in obs.get("events", []):
            if ev.get("type") == "damaged" and ev.get("bearing") is not None:
                self.last_damage_bearing = ev["bearing"]
                self.last_damage_t = t

        if self.last_pos is not None and dist2d(pos, self.last_pos) < 0.25:
            self.stuck_ticks += 1
        else:
            self.stuck_ticks = 0
        self.last_pos = pos

        self.strafe_timer += 1
        if self.strafe_timer > 16:
            self.strafe_timer = 0
            self.strafe_dir = -self.strafe_dir

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

        # 2. Weapon pickup / swap
        held_kinds = {w["kind"] for w in weapons if w is not None}
        empty_slot = 0 if weapons[0] is None else (1 if weapons[1] is None else None)
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

        # 4. Target Selection (prefer enemies with clear LOS, then closest/lowest HP)
        target = None
        target_type = None
        target_los = False

        if enemies:
            scored_enemies = []
            for e in enemies:
                ep = e["pos"]
                ec = (ep[0], ep[1], ep[2] + 4.5)
                d = dist2d(pos, ep)
                los = self.map.has_los(muzzle_pos, ec)
                hp_w = 0.65 + 0.35 * (e.get("hp", 1000) / max(e.get("max_hp", 1400), 1.0))
                score = d * hp_w + (0.0 if los else 120.0)
                scored_enemies.append((score, los, e))
            scored_enemies.sort(key=lambda x: x[0])
            _, target_los, target = scored_enemies[0]
            target_type = "mech"
        elif npcs:
            nearest_npc = min(npcs, key=lambda n: dist2d(pos, n["pos"]))
            if dist2d(pos, nearest_npc["pos"]) < 85.0:
                target = nearest_npc
                target_type = "npc"
                target_los = self.map.has_los(
                    muzzle_pos, (target["pos"][0], target["pos"][1], target["pos"][2] + 1.8)
                )
        if target is None and crates:
            nearest_crate = min(crates, key=lambda c: dist2d(pos, c["pos"]))
            if dist2d(pos, nearest_crate["pos"]) < 65.0:
                target = nearest_crate
                target_type = "crate"
                target_los = self.map.has_los(
                    muzzle_pos, (target["pos"][0], target["pos"][1], target["pos"][2] + 1.2)
                )

        # 5. Movement Decision
        move_x, move_y = 0.0, 0.0
        want_boost = False

        zc = zone["next_center"]
        zr = zone["next_radius"]
        d_zone = dist2d(pos, zc)
        outside_next = d_zone > max(zr - 18.0, 5.0)
        urgent_zone = (not me["in_zone"]) or (d_zone > zr - 8.0 and t > zone["shrink_start"] - 12.0)

        best_drop = None
        for dr in drops:
            if dr["lands_in"] < 12.0 and dist2d(pos, dr["pos"]) < 95.0:
                if not enemies or dist2d(pos, dr["pos"]) < 25.0:
                    best_drop = dr
                    break

        if urgent_zone:
            move_x = zc[0] - pos[0]
            move_y = zc[1] - pos[1]
            want_boost = energy > 25.0
        elif target_type == "mech":
            ex, ey = target["pos"][0], target["pos"][1]
            dx, dy = ex - pos[0], ey - pos[1]
            d = max(math.hypot(dx, dy), 1e-4)
            nx, ny = dx / d, dy / d
            sx, sy = -ny * self.strafe_dir, nx * self.strafe_dir

            has_lightning = "lightning" in held_kinds
            enemy_weapons = set(target.get("weapons", []))
            # Close in if we have lightning (especially vs laser), otherwise mid-range kite
            ideal_dist = 30.0 if (has_lightning and hp_frac > 0.42) else 65.0
            if "laser" in enemy_weapons and has_lightning and hp_frac > 0.35:
                ideal_dist = 28.0

            if hp_frac < 0.36:
                move_x = -nx * 0.85 + sx * 0.55
                move_y = -ny * 0.85 + sy * 0.55
                want_boost = energy > 20.0
            elif d > ideal_dist + 10.0:
                move_x = nx * 0.65 + sx * 0.75
                move_y = ny * 0.65 + sy * 0.75
                want_boost = has_lightning and d < 65.0 and energy > 35.0
            elif d < ideal_dist - 10.0:
                move_x = -nx * 0.65 + sx * 0.75
                move_y = -ny * 0.65 + sy * 0.75
            else:
                move_x, move_y = sx, sy

            # Dodge charging laser or close projectiles
            if target.get("charging"):
                move_x, move_y = sx, sy
                want_boost = energy > 15.0
            for proj in obs.get("projectiles", []):
                if dist2d(pos, proj["pos"]) < 28.0:
                    pvx, pvy = proj["vel"][0], proj["vel"][1]
                    pm = math.hypot(pvx, pvy)
                    if pm > 1.0:
                        move_x += (-pvy / pm) * self.strafe_dir * 0.9
                        move_y += (pvx / pm) * self.strafe_dir * 0.9
                        want_boost = energy > 20.0
                        break
        elif cubes:
            c = min(cubes, key=lambda x: dist2d(pos, x["pos"]))
            move_x = c["pos"][0] - pos[0]
            move_y = c["pos"][1] - pos[1]
            want_boost = energy > 50.0 and dist2d(pos, c["pos"]) > 15.0
        elif empty_slot is not None and pickups:
            valid_p = [p for p in pickups if p["kind"] not in held_kinds]
            if valid_p:
                p = min(valid_p, key=lambda x: dist2d(pos, x["pos"]))
                move_x = p["pos"][0] - pos[0]
                move_y = p["pos"][1] - pos[1]
                want_boost = energy > 50.0
        elif best_drop is not None:
            move_x = best_drop["pos"][0] - pos[0]
            move_y = best_drop["pos"][1] - pos[1]
            want_boost = energy > 45.0 and dist2d(pos, best_drop["pos"]) > best_drop["radius"]
        elif target_type in ("npc", "crate"):
            dx = target["pos"][0] - pos[0]
            dy = target["pos"][1] - pos[1]
            d = max(math.hypot(dx, dy), 1e-4)
            if d > 30.0:
                move_x, move_y = dx, dy
            else:
                move_x, move_y = -dy * self.strafe_dir, dx * self.strafe_dir
        elif outside_next:
            move_x = zc[0] - pos[0]
            move_y = zc[1] - pos[1]
        else:
            if self.roam_target is None or dist2d(pos, self.roam_target) < 15.0:
                ang = self.rng.uniform(0.0, 2.0 * math.pi)
                rad = self.rng.uniform(0.15, 0.75) * max(zr - 15.0, 10.0)
                self.roam_target = (
                    clamp(zc[0] + math.cos(ang) * rad, self.bounds_x0(), self.bounds_x1()),
                    clamp(zc[1] + math.sin(ang) * rad, self.bounds_y0(), self.bounds_y1()),
                )
            move_x = self.roam_target[0] - pos[0]
            move_y = self.roam_target[1] - pos[1]

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

        # 6. Aiming & Firing
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
                    l_range = wstats.get("range", 45.0)
                    if target_los and d_target <= l_range - 1.0 and yaw_err < 0.45 and energy > 8.0:
                        fire_flags[s_idx] = True

                elif kind == "gun":
                    g_range = wstats.get("range", 230.0)
                    if target_los and d_target <= g_range and yaw_err < 0.16 and w.get("reload", 0.0) <= 0.0:
                        fire_flags[s_idx] = True

                elif kind == "laser":
                    l_range = wstats.get("range", 280.0)
                    charge = w.get("charge", 0.0)
                    full_charge = wstats.get("charge", 0.75)
                    min_charge = wstats.get("min_charge", 0.15)
                    if target_type == "mech" and target_los and d_target <= l_range:
                        des_yaw, des_pitch = self.aim_at_point(muzzle_pos, aim_center)
                        yaw_err = abs(angle_diff(des_yaw, cur_yaw))
                        if charge == 0.0:
                            if w.get("cooldown", 0.0) <= 0.0 and yaw_err < 0.45:
                                fire_flags[s_idx] = True
                        else:
                            if (charge >= full_charge * 0.75 and yaw_err < 0.08) or (
                                charge >= min_charge and target.get("hp", 1000) < 160 and yaw_err < 0.08
                            ):
                                fire_flags[s_idx] = False
                            else:
                                fire_flags[s_idx] = True

                elif kind == "mortar":
                    # Mortar can lob over buildings even without direct LOS
                    if d_target <= 215.0 and d_target >= 18.0 and w.get("cooldown", 0.0) <= 0.0:
                        m_speed = wstats.get("speed", 72.0)
                        m_grav = wstats.get("gravity", 22.0)
                        des_pitch = self.mortar_pitch(muzzle_pos, aim_center, m_speed, m_grav)
                        if yaw_err < 0.15:
                            fire_flags[s_idx] = True

            action["aim"] = [round(des_yaw, 4), round(des_pitch, 4)]
            action["fire"] = fire_flags
            mf.draw.line(list(muzzle_pos), list(aim_center), "#ff5040" if target_los else "#ffaa00")
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
                    if w.get("ammo", mag) < mag * 0.5:
                        action["reload"] = True

        mf.draw.label(f"hp {me['hp']:.0f} | cubes {me['cubes']} | {target_type or 'roam'}")
        return action

    def bounds_x0(self): return self.map.bounds[0]
    def bounds_x1(self): return self.map.bounds[1]
    def bounds_y0(self): return self.map.bounds[2]
    def bounds_y1(self): return self.map.bounds[3]


if __name__ == "__main__":
    mf.run(Bot)