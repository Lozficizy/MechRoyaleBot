#!/usr/bin/env python3
"""
ApexMech v3.0 - Mech Royale Bot (mechfight/1)
Ultra-fast (<2ms/tick), kill-focus cube snowball, anti-swarm kiting, dual-wield DPS.
Standard library only.
"""
import json
import math
import os
import random
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
    """Lightweight spatial-grid lookup for prisms (buildings/stadium/bridges)."""

    def __init__(self, map_dir: str):
        self.cell_size = 45.0
        self.grid: dict[tuple[int, int], list[int]] = {}
        self.prisms: list[dict] = []
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
                mf.log(f"[map] load error: {e}")

    def point_in_prism(self, x: float, y: float, z: float) -> bool:
        cand = self.grid.get((int(math.floor(x / self.cell_size)), int(math.floor(y / self.cell_size))))
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
        cand = self.grid.get((int(math.floor(x / self.cell_size)), int(math.floor(y / self.cell_size))))
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
        """Fast 3D LOS check (capped at 16 steps for <0.1ms runtime)."""
        d = dist3d(a, b)
        if d < 6.0:
            return True
        steps = min(int(d / 8.0), 16)
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
        for off in (0.0, 0.4, -0.4, 0.8, -0.8, 1.25, -1.25, 1.7, -1.7, 2.2, -2.2):
            ang = base_ang + off
            ca, sa = math.cos(ang), math.sin(ang)
            if not self.is_blocked_body(x + ca * 5.0, y + sa * 5.0, z):
                return ca, sa
        return desired_dx / mag, desired_dy / mag


class Bot:
    name = "ApexMech-v3.0"

    def __init__(self, hello: mf.Hello):
        self.slot = hello["slot"]
        self.rng = random.Random(hello["seed"])
        self.config = hello["config"]
        self.map = ArenaMap(hello["map"]["dir"])
        self.pickup_r = self.config.get("loot", {}).get("pickup_radius", 4.0)

        # Lightning is king vs stacked bots (chain + 240 DPS + shorts lasers)
        self.weapon_rank = {"lightning": 4, "laser": 3, "gun": 2, "mortar": 1}

        # Survival + DPS upgrades
        self.upgrade_weights = {
            "armor": 100,
            "hp": 97,
            "regen": 95,
            "lightning": 94,
            "gun_rate": 91,
            "gun_damage": 90,
            "laser": 85,
            "speed": 83,
            "boost": 80,
            "energy": 78,
            "reload": 72,
            "sensors": 60,
            "mortar": 35,
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
        return math.atan2(dy, dx), clamp(math.atan2(dz, max(horiz, 1e-4)), -1.2, 1.2)

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

    def incoming_projectile_dodge(self, pos, projectiles):
        mx, my = pos[0], pos[1]
        for p in projectiles:
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
            if t_cpa > 0.6:
                continue
            cpa_x = px + vx * t_cpa - mx
            cpa_y = py + vy * t_cpa - my
            if cpa_x * cpa_x + cpa_y * cpa_y < 5.2 * 5.2:
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
        stats = me.get("stats", {})
        muzzle_pos = (pos[0], pos[1], pos[2] + stats.get("muzzle_height", 7.0))
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
        if self.strafe_timer > 14:
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

        # 2. Auto-pickup / swap nearby weapon
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
        zc = zone["next_center"]
        zr = zone["next_radius"]

        # 4. Fast Target Selection (only raycast top 3 closest enemies)
        target = None
        target_type = None
        target_los = False
        near_enemies = []

        if enemies:
            enemies.sort(key=lambda e: dist2d(pos, e["pos"]))
            near_enemies = [e for e in enemies if dist2d(pos, e["pos"]) < 85.0]
            candidates = []
            for e in enemies[:3]:
                ep = e["pos"]
                d = dist2d(pos, ep)
                los = self.map.has_los(muzzle_pos, (ep[0], ep[1], ep[2] + 4.5))
                # Strongly prioritize low-HP enemies to secure kills & cube heals!
                hp_ratio = e.get("hp", 1000.0) / max(e.get("max_hp", 1400.0), 1.0)
                score = d * (0.4 + 0.6 * hp_ratio) + (0.0 if los else 150.0)
                candidates.append((score, los, e))
            candidates.sort(key=lambda x: x[0])
            _, target_los, target = candidates[0]
            target_type = "mech"
        elif npcs:
            nearest_npc = min(npcs, key=lambda n: dist2d(pos, n["pos"]))
            if dist2d(pos, nearest_npc["pos"]) < 90.0:
                target = nearest_npc
                target_type = "npc"
                target_los = self.map.has_los(
                    muzzle_pos, (target["pos"][0], target["pos"][1], target["pos"][2] + 1.8)
                )
        if target is None and crates:
            nearest_crate = min(crates, key=lambda c: dist2d(pos, c["pos"]))
            if dist2d(pos, nearest_crate["pos"]) < 75.0:
                target = nearest_crate
                target_type = "crate"
                target_los = self.map.has_los(
                    muzzle_pos, (target["pos"][0], target["pos"][1], target["pos"][2] + 1.2)
                )

        # 5. Movement Decision
        move_x, move_y = 0.0, 0.0
        want_boost = False

        d_zone = dist2d(pos, zc)
        outside_next = d_zone > max(zr - 18.0, 5.0)
        urgent_zone = (not me["in_zone"]) or (d_zone > zr - 8.0 and t > zone["shrink_start"] - 12.0)

        # Nearby cube for instant heal + damage buff
        near_cube = None
        if cubes:
            c = min(cubes, key=lambda x: dist2d(pos, x["pos"]))
            if dist2d(pos, c["pos"]) < (32.0 if enemies else 90.0):
                near_cube = c

        # Nearby lightning pickup (highest priority weapon)
        near_lightning = None
        if "lightning" not in held_kinds and pickups:
            lp = [p for p in pickups if p["kind"] == "lightning"]
            if lp:
                p = min(lp, key=lambda x: dist2d(pos, x["pos"]))
                if dist2d(pos, p["pos"]) < (40.0 if enemies else 110.0):
                    near_lightning = p

        # Supply drop (full heal + 3 cubes)
        best_drop = None
        for dr in drops:
            dd = dist2d(pos, dr["pos"])
            if dr["lands_in"] < 16.0 and dd < (140.0 if (hp_frac < 0.72 or me["cubes"] < 6) else 90.0):
                if len(near_enemies) == 0 or dd < 24.0:
                    best_drop = dr
                    break

        if urgent_zone:
            move_x = zc[0] - pos[0]
            move_y = zc[1] - pos[1]
            want_boost = energy > 22.0
        elif near_cube is not None and (not near_enemies or dist2d(pos, near_cube["pos"]) < 18.0):
            move_x = near_cube["pos"][0] - pos[0]
            move_y = near_cube["pos"][1] - pos[1]
            want_boost = energy > 35.0
        elif near_lightning is not None and (not near_enemies or dist2d(pos, near_lightning["pos"]) < 22.0):
            move_x = near_lightning["pos"][0] - pos[0]
            move_y = near_lightning["pos"][1] - pos[1]
            want_boost = energy > 35.0
        elif target_type == "mech":
            has_lightning = "lightning" in held_kinds
            if len(near_enemies) >= 2:
                # Anti-swarm kiting: backpedal away from the pack's centroid while strafing
                cx = sum(e["pos"][0] for e in near_enemies) / len(near_enemies)
                cy = sum(e["pos"][1] for e in near_enemies) / len(near_enemies)
                dx, dy = pos[0] - cx, pos[1] - cy
                d = max(math.hypot(dx, dy), 1e-4)
                nx, ny = dx / d, dy / d
                sx, sy = -ny * self.strafe_dir, nx * self.strafe_dir
                # Pull slightly toward zone center so we don't back into a dead-end corner
                zx, zy = zc[0] - pos[0], zc[1] - pos[1]
                zm = max(math.hypot(zx, zy), 1e-4)
                move_x = nx * 0.75 + sx * 0.55 + (zx / zm) * 0.25
                move_y = ny * 0.75 + sy * 0.55 + (zy / zm) * 0.25
                want_boost = energy > 28.0 and dist2d(pos, near_enemies[0]["pos"]) < 36.0
            else:
                ex, ey = target["pos"][0], target["pos"][1]
                dx, dy = ex - pos[0], ey - pos[1]
                d = max(math.hypot(dx, dy), 1e-4)
                nx, ny = dx / d, dy / d
                sx, sy = -ny * self.strafe_dir, nx * self.strafe_dir

                ideal_dist = 36.0 if (has_lightning and hp_frac > 0.35) else 68.0
                if d > ideal_dist + 8.0:
                    move_x = nx * 0.65 + sx * 0.75
                    move_y = ny * 0.65 + sy * 0.75
                    want_boost = has_lightning and d < 62.0 and energy > 40.0
                elif d < ideal_dist - 8.0:
                    move_x = -nx * 0.70 + sx * 0.70
                    move_y = -ny * 0.70 + sy * 0.70
                    want_boost = hp_frac < 0.45 and energy > 25.0
                else:
                    move_x, move_y = sx, sy

            if target.get("charging"):
                ex, ey = target["pos"][0], target["pos"][1]
                dx, dy = ex - pos[0], ey - pos[1]
                d = max(math.hypot(dx, dy), 1e-4)
                move_x, move_y = (-dy / d) * self.strafe_dir, (dx / d) * self.strafe_dir
                want_boost = energy > 15.0
        elif empty_slot is not None and pickups:
            valid_p = [p for p in pickups if p["kind"] not in held_kinds]
            if valid_p:
                p = min(valid_p, key=lambda x: dist2d(pos, x["pos"]))
                move_x = p["pos"][0] - pos[0]
                move_y = p["pos"][1] - pos[1]
                want_boost = energy > 45.0
        elif best_drop is not None:
            move_x = best_drop["pos"][0] - pos[0]
            move_y = best_drop["pos"][1] - pos[1]
            want_boost = energy > 40.0 and dist2d(pos, best_drop["pos"]) > best_drop["radius"]
        elif target_type in ("npc", "crate"):
            dx = target["pos"][0] - pos[0]
            dy = target["pos"][1] - pos[1]
            d = max(math.hypot(dx, dy), 1e-4)
            if d > 28.0:
                move_x, move_y = dx, dy
            else:
                move_x, move_y = -dy * self.strafe_dir, dx * self.strafe_dir
        elif outside_next:
            move_x = zc[0] - pos[0]
            move_y = zc[1] - pos[1]
        else:
            if self.roam_target is None or dist2d(pos, self.roam_target) < 15.0:
                ang = self.rng.uniform(0.0, 2.0 * math.pi)
                rad = self.rng.uniform(0.15, 0.72) * max(zr - 15.0, 10.0)
                self.roam_target = (
                    clamp(zc[0] + math.cos(ang) * rad, self.map.bounds[0], self.map.bounds[1]),
                    clamp(zc[1] + math.sin(ang) * rad, self.map.bounds[2], self.map.bounds[3]),
                )
            move_x = self.roam_target[0] - pos[0]
            move_y = self.roam_target[1] - pos[1]

        dodge = self.incoming_projectile_dodge(pos, obs.get("projectiles", []))
        if dodge is not None:
            move_x = move_x * 0.35 + dodge[0] * 0.95
            move_y = move_y * 0.35 + dodge[1] * 0.95
            want_boost = energy > 18.0

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
                    if target_los and d_target <= l_range - 1.0 and yaw_err < 0.48 and energy > 5.0:
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
                            if w.get("cooldown", 0.0) <= 0.0 and yaw_err < 0.45 and (d_target > 40.0 or "mortar" in target.get("weapons", [])):
                                fire_flags[s_idx] = True
                        else:
                            if (charge >= full_charge * 0.72 and yaw_err < 0.08) or (
                                charge >= min_charge and (target.get("hp", 1000) < 160 or d_target < 35.0) and yaw_err < 0.09
                            ):
                                fire_flags[s_idx] = False
                            else:
                                fire_flags[s_idx] = True

                elif kind == "mortar":
                    if d_target <= 215.0 and d_target >= 20.0 and w.get("cooldown", 0.0) <= 0.0:
                        m_speed = wstats.get("speed", 72.0)
                        m_grav = wstats.get("gravity", 22.0)
                        des_pitch = self.mortar_pitch(muzzle_pos, aim_center, m_speed, m_grav)
                        if yaw_err < 0.15:
                            fire_flags[s_idx] = True

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
                    if w.get("ammo", mag) < mag * 0.6:
                        action["reload"] = True

        mf.draw.label(f"v3 | hp {me['hp']:.0f} | c {me['cubes']} | {target_type or 'roam'}")
        return action


if __name__ == "__main__":
    mf.run(Bot)