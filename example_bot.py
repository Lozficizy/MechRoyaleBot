#!/usr/bin/env python3
"""
ApexMech v2.0 - Mech Royale Bot (mechfight/1)
Cover-seeking regen, 1v1 LOS funneling, true projectile dodging, dual-wield DPS.
Standard library only.
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
    """Fast spatial-grid lookup for prisms (buildings/stadium/bridges) and terrain."""

    def __init__(self, map_dir: str):
        self.cell_size = 40.0
        self.grid: dict[tuple[int, int], list[int]] = {}
        self.prisms: list[dict] = []
        self.bounds = (-508.0, 551.0, -329.0, 384.0)
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

    def has_los(self, a, b, step_m: float = 5.0, max_steps: int = 36) -> bool:
        d = dist3d(a, b)
        if d < 5.0:
            return True
        steps = min(int(d / step_m), max_steps)
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

    def find_cover_direction(self, pos, enemies, zone_center, zone_radius, want_zero_los: bool):
        """Sample 12 directions around mech to minimize enemy LOS while staying in zone."""
        x, y, z = pos[0], pos[1], pos[2]
        top_enemies = sorted(enemies, key=lambda e: dist2d(pos, e["pos"]))[:4]
        best_score = 1e9
        best_dir = None

        for k in range(12):
            ang = k * (math.pi / 6.0)
            ca, sa = math.cos(ang), math.sin(ang)
            # Check immediate walkability
            if self.is_blocked_body(x + ca * 5.0, y + sa * 5.0, z):
                continue
            tx, ty = x + ca * 18.0, y + sa * 18.0
            if self.is_blocked_body(tx, ty, z):
                tx, ty = x + ca * 11.0, y + sa * 11.0
                if self.is_blocked_body(tx, ty, z):
                    continue

            tz = self.ground_z(tx, ty) + 6.5
            # Count how many enemies have LOS to this candidate spot
            los_cnt = 0
            min_edist = 999.0
            for e in top_enemies:
                ep = e["pos"]
                ed = dist2d((tx, ty), ep)
                if ed < min_edist:
                    min_edist = ed
                if self.has_los((tx, ty, tz), (ep[0], ep[1], ep[2] + 7.0), step_m=7.0, max_steps=20):
                    los_cnt += 1

            d_zone = dist2d((tx, ty), zone_center)
            zone_pen = max(0.0, d_zone - (zone_radius - 12.0)) * 4.0

            if want_zero_los:
                # Strongly prefer 0 LOS and distance away from enemies
                score = los_cnt * 120.0 - min(min_edist, 90.0) * 0.9 + zone_pen
            else:
                # Prefer 1 LOS (funneling) or 0 LOS, avoiding 2+ LOS
                los_pen = 0.0 if los_cnt == 1 else (30.0 if los_cnt == 0 else los_cnt * 100.0)
                score = los_pen - min(min_edist, 70.0) * 0.4 + zone_pen

            if score < best_score:
                best_score = score
                best_dir = (ca, sa)

        return best_dir


class Bot:
    name = "ApexMech-v2.0"

    def __init__(self, hello: mf.Hello):
        self.slot = hello["slot"]
        self.rng = random.Random(hello["seed"])
        self.config = hello["config"]
        self.map = ArenaMap(hello["map"]["dir"])
        self.pickup_r = self.config.get("loot", {}).get("pickup_radius", 4.0)

        # Lightning is #1 for 1v15 horde (240 DPS + chain + shorts lasers), Laser #2, Gun #3
        self.weapon_rank = {"lightning": 4, "laser": 3, "gun": 2, "mortar": 1}

        # Upgrade preference keywords (survival & sustained DPS > everything)
        self.upgrade_weights = {
            "armor": 100,
            "regen": 98,
            "hp": 96,
            "lightning": 94,
            "gun_rate": 90,
            "gun_damage": 89,
            "laser": 85,
            "speed": 82,
            "boost": 80,
            "energy": 78,
            "reload": 70,
            "sensors": 62,
            "mortar": 40,
        }

        self.last_pos = None
        self.stuck_ticks = 0
        self.strafe_dir = 1.0
        self.strafe_timer = 0
        self.last_damage_bearing = None
        self.last_damage_t = -10.0
        self.roam_target = None
        self.regen_mode = False

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

    def incoming_projectile_dodge(self, pos, projectiles):
        """Return perpendicular dodge vector only if a projectile is heading straight at us."""
        mx, my = pos[0], pos[1]
        for p in projectiles:
            px, py = p["pos"][0], p["pos"][1]
            vx, vy = p["vel"][0], p["vel"][1]
            spd2 = vx * vx + vy * vy
            if spd2 < 10.0:
                continue
            rx, ry = mx - px, my - py
            dist2 = rx * rx + ry * ry
            if dist2 > 45.0 * 45.0:
                continue
            dot = rx * vx + ry * vy
            if dot <= 0.0:
                continue  # Moving away
            t_cpa = dot / spd2
            if t_cpa > 0.65:
                continue
            cpa_x = px + vx * t_cpa - mx
            cpa_y = py + vy * t_cpa - my
            if cpa_x * cpa_x + cpa_y * cpa_y < 5.5 * 5.5:
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
        if self.strafe_timer > 15:
            self.strafe_timer = 0
            self.strafe_dir = -self.strafe_dir

        # Hysteresis for regen mode: disengage below 48% HP, re-engage above 82% HP
        if hp_frac < 0.48:
            self.regen_mode = True
        elif hp_frac > 0.82:
            self.regen_mode = False

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
        zc = zone["next_center"]
        zr = zone["next_radius"]

        # 4. Evaluate enemies & LOS count
        los_enemies = []
        scored_enemies = []
        for e in enemies:
            ep = e["pos"]
            ec = (ep[0], ep[1], ep[2] + 4.5)
            d = dist2d(pos, ep)
            los = self.map.has_los(muzzle_pos, ec)
            if los:
                los_enemies.append(e)
            hp_w = 0.55 + 0.45 * (e.get("hp", 1000) / max(e.get("max_hp", 1400), 1.0))
            score = d * hp_w + (0.0 if los else 140.0)
            scored_enemies.append((score, los, e))

        target = None
        target_type = None
        target_los = False

        if scored_enemies:
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
            if dist2d(pos, nearest_crate["pos"]) < 70.0:
                target = nearest_crate
                target_type = "crate"
                target_los = self.map.has_los(
                    muzzle_pos, (target["pos"][0], target["pos"][1], target["pos"][2] + 1.2)
                )

        # If target is almost dead (< 180 HP) and in LOS, finish them even in regen mode
        finishing_kill = (
            target_type == "mech" and target_los and target.get("hp", 1000) < 180.0
        )
        actively_regening = self.regen_mode and not finishing_kill

        # 5. Movement Decision
        move_x, move_y = 0.0, 0.0
        want_boost = False

        d_zone = dist2d(pos, zc)
        outside_next = d_zone > max(zr - 18.0, 5.0)
        urgent_zone = (not me["in_zone"]) or (d_zone > zr - 8.0 and t > zone["shrink_start"] - 12.0)

        # Check supply drops (full heal + 3 cubes!)
        best_drop = None
        for dr in drops:
            dd = dist2d(pos, dr["pos"])
            if dr["lands_in"] < 18.0 and dd < (150.0 if (hp_frac < 0.75 or me["cubes"] < 8) else 95.0):
                if len(los_enemies) == 0 or dd < 25.0:
                    best_drop = dr
                    break

        if urgent_zone:
            move_x = zc[0] - pos[0]
            move_y = zc[1] - pos[1]
            want_boost = energy > 22.0
        elif actively_regening and enemies:
            # Seek 0-LOS cover behind buildings to trigger 45 HP/s regen
            cover_dir = self.map.find_cover_direction(pos, enemies, zc, zr, want_zero_los=True)
            if cover_dir is not None:
                move_x, move_y = cover_dir
            else:
                # Retreat away from nearest enemy
                ne = min(enemies, key=lambda e: dist2d(pos, e["pos"]))
                move_x = pos[0] - ne["pos"][0]
                move_y = pos[1] - ne["pos"][1]
            want_boost = energy > 20.0 and len(los_enemies) > 0
        elif len(los_enemies) >= 2:
            # Swarm alert: funnel enemies around cover so only 1 can shoot us
            cover_dir = self.map.find_cover_direction(pos, enemies, zc, zr, want_zero_los=False)
            if cover_dir is not None:
                move_x, move_y = cover_dir
                want_boost = energy > 25.0
            else:
                cx = sum(e["pos"][0] for e in los_enemies) / len(los_enemies)
                cy = sum(e["pos"][1] for e in los_enemies) / len(los_enemies)
                move_x, move_y = pos[0] - cx, pos[1] - cy
                want_boost = energy > 25.0
        elif target_type == "mech":
            ex, ey = target["pos"][0], target["pos"][1]
            dx, dy = ex - pos[0], ey - pos[1]
            d = max(math.hypot(dx, dy), 1e-4)
            nx, ny = dx / d, dy / d
            sx, sy = -ny * self.strafe_dir, nx * self.strafe_dir

            has_lightning = "lightning" in held_kinds
            ideal_dist = 32.0 if (has_lightning and hp_frac > 0.50) else 62.0

            if d > ideal_dist + 10.0:
                move_x = nx * 0.65 + sx * 0.75
                move_y = ny * 0.65 + sy * 0.75
                want_boost = has_lightning and d < 62.0 and energy > 38.0
            elif d < ideal_dist - 10.0:
                move_x = -nx * 0.65 + sx * 0.75
                move_y = -ny * 0.65 + sy * 0.75
            else:
                move_x, move_y = sx, sy

            if target.get("charging"):
                move_x, move_y = sx, sy
                want_boost = energy > 15.0
        elif cubes:
            c = min(cubes, key=lambda x: dist2d(pos, x["pos"]))
            move_x = c["pos"][0] - pos[0]
            move_y = c["pos"][1] - pos[1]
            want_boost = energy > 45.0 and dist2d(pos, c["pos"]) > 14.0
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

        # Overlay true projectile dodge if something is about to hit us
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

            # Hold fire ONLY if we are safely out of LOS in regen mode (so 5s regen timer runs!)
            hold_fire_for_regen = actively_regening and len(los_enemies) == 0

            fire_flags = [False, False]
            if not hold_fire_for_regen:
                for s_idx, w in enumerate(weapons):
                    if w is None:
                        continue
                    kind = w["kind"]
                    wstats = w.get("stats", {})

                    if kind == "lightning":
                        l_range = wstats.get("range", 45.0)
                        if target_los and d_target <= l_range - 1.0 and yaw_err < 0.48 and energy > 6.0:
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
                        enemy_has_mortar = "mortar" in target.get("weapons", [])
                        safe_to_charge = (len(los_enemies) <= 1 and d_target > 42.0) or enemy_has_mortar
                        if target_type == "mech" and target_los and d_target <= l_range:
                            des_yaw, des_pitch = self.aim_at_point(muzzle_pos, aim_center)
                            yaw_err = abs(angle_diff(des_yaw, cur_yaw))
                            if charge == 0.0:
                                if safe_to_charge and w.get("cooldown", 0.0) <= 0.0 and yaw_err < 0.45:
                                    fire_flags[s_idx] = True
                            else:
                                if (charge >= full_charge * 0.75 and yaw_err < 0.08) or (
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
                    if w.get("ammo", mag) < mag * 0.6:
                        action["reload"] = True

        state = "REGEN" if actively_regening else ("SWARM" if len(los_enemies) >= 2 else (target_type or "ROAM"))
        mf.draw.label(f"v2 | hp {me['hp']:.0f} | c {me['cubes']} | {state}")
        return action


if __name__ == "__main__":
    mf.run(Bot)