#!/usr/bin/env python3
"""
ApexMech v4.0 - Rooftop Camper & Edge-Peeker (mechfight/1)
Finds climbable rooftops inside the next zone, jets onto them, stays silent,
peeks edges to snipe, and steps back to roof center to trigger 45 HP/s regen.
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


def point_in_prism_2d(x: float, y: float, p: dict) -> bool:
    minx, miny, maxx, maxy = p["bbox"]
    if x < minx or x > maxx or y < miny or y > maxy:
        return False
    if not point_in_ring(x, y, p["ring"]):
        return False
    for h in p["holes"]:
        if point_in_ring(x, y, h):
            return False
    return True


class ArenaMap:
    """Spatial-grid lookup for prisms + precomputed campable rooftops."""

    def __init__(self, map_dir: str):
        self.cell_size = 45.0
        self.grid: dict[tuple[int, int], list[int]] = {}
        self.prisms: list[dict] = []
        self.rooftops: list[dict] = []
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
                        "kind": p.get("kind", "building"),
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

                    # Precompute good campable rooftops (10m..32m tall, wide enough to step back)
                    w = maxx - minx
                    h = maxy - miny
                    if prism["kind"] == "building" and 10.0 <= prism["top"] <= 32.0 and w >= 14.0 and h >= 14.0:
                        cx = 0.5 * (minx + maxx)
                        cy = 0.5 * (miny + maxy)
                        if point_in_prism_2d(cx, cy, prism):
                            self.rooftops.append({
                                "idx": idx,
                                "cx": cx,
                                "cy": cy,
                                "top": prism["top"],
                                "prism": prism,
                            })
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
            if point_in_prism_2d(x, y, p):
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
            if point_in_prism_2d(x, y, p):
                return True
        return False

    def has_los(self, a, b) -> bool:
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

    def pick_best_rooftop(self, pos, zc, zr):
        """Pick closest campable rooftop comfortably inside the next zone."""
        safe_r = max(zr - 16.0, 8.0)
        best = None
        best_d = 1e9
        for r in self.rooftops:
            if dist2d((r["cx"], r["cy"]), zc) <= safe_r:
                d = dist2d(pos, (r["cx"], r["cy"]))
                if d < best_d:
                    best_d = d
                    best = r
        return best


class Bot:
    name = "ApexMech-v4.0"

    def __init__(self, hello: mf.Hello):
        self.slot = hello["slot"]
        self.rng = random.Random(hello["seed"])
        self.config = hello["config"]
        self.map = ArenaMap(hello["map"]["dir"])
        self.pickup_r = self.config.get("loot", {}).get("pickup_radius", 4.0)

        # Rooftop camper weapon priority: Laser (sniping) > Lightning (anti-rush) > Mortar > Gun
        self.weapon_rank = {"laser": 5, "lightning": 4, "mortar": 3, "gun": 2}

        # Survival & regen upgrades first
        self.upgrade_weights = {
            "armor": 100,
            "regen": 99,
            "hp": 97,
            "laser": 93,
            "lightning": 92,
            "gun_damage": 88,
            "gun_rate": 87,
            "energy": 84,
            "boost": 80,
            "speed": 78,
            "sensors": 75,
            "reload": 70,
            "mortar": 65,
        }

        self.last_pos = None
        self.stuck_ticks = 0
        self.strafe_dir = 1.0
        self.strafe_timer = 0
        self.last_damage_bearing = None
        self.last_damage_t = -10.0
        self.regen_mode = False
        self.climbing = False
        self.target_roof = None

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

        if self.last_pos is not None and dist2d(pos, self.last_pos) < 0.2:
            self.stuck_ticks += 1
        else:
            self.stuck_ticks = 0
        self.last_pos = pos

        self.strafe_timer += 1
        if self.strafe_timer > 16:
            self.strafe_timer = 0
            self.strafe_dir = -self.strafe_dir

        # Regen hysteresis: duck back to heal below 55% HP, re-peek above 88% HP
        if hp_frac < 0.55:
            self.regen_mode = True
        elif hp_frac > 0.88:
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

        # 2. Auto-pickup nearby weapon
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

        # 3. Zone & Rooftop Selection
        zone = obs["zone"]
        zc = zone["next_center"]
        zr = zone["next_radius"]

        if self.target_roof is None or dist2d((self.target_roof["cx"], self.target_roof["cy"]), zc) > max(zr - 12.0, 6.0):
            self.target_roof = self.map.pick_best_rooftop(pos, zc, zr)

        on_roof = False
        if self.target_roof is not None:
            if pos[2] >= self.target_roof["top"] - 1.8 and point_in_prism_2d(pos[0], pos[1], self.target_roof["prism"]):
                on_roof = True
                self.climbing = False

        # 4. Classify visible enemies (ignore NPCs & crates while camping to stay quiet!)
        enemies = [m for m in obs.get("mechs", []) if m["kind"] == "mech"]
        cubes = obs.get("cubes", [])
        pickups = obs.get("pickups", [])

        target = None
        target_los = False
        enemies_with_los = 0
        if enemies:
            enemies.sort(key=lambda e: dist2d(pos, e["pos"]))
            candidates = []
            for e in enemies[:3]:
                ep = e["pos"]
                d = dist2d(pos, ep)
                los = self.map.has_los(muzzle_pos, (ep[0], ep[1], ep[2] + 4.5))
                if los:
                    enemies_with_los += 1
                hp_ratio = e.get("hp", 1000.0) / max(e.get("max_hp", 1400.0), 1.0)
                score = d * (0.45 + 0.55 * hp_ratio) + (0.0 if los else 140.0)
                candidates.append((score, los, e))
            candidates.sort(key=lambda x: x[0])
            _, target_los, target = candidates[0]

        # 5. Movement: Roof Camp > Climb Roof > Silent Rotate
        move_x, move_y = 0.0, 0.0
        want_boost = False
        want_jump = False
        silent_walk = False

        d_zone = dist2d(pos, zc)
        urgent_zone = (not me["in_zone"]) or (d_zone > zr - 8.0 and t > zone["shrink_start"] - 12.0)

        if urgent_zone:
            move_x = zc[0] - pos[0]
            move_y = zc[1] - pos[1]
            want_boost = energy > 22.0
        elif on_roof:
            rcx, rcy = self.target_roof["cx"], self.target_roof["cy"]
            d_center = dist2d(pos, (rcx, rcy))
            # Check if a cube or weapon is right on our roof
            roof_loot = None
            for c in cubes:
                if abs(c["pos"][2] - pos[2]) < 3.0 and dist2d(pos, c["pos"]) < 18.0 and point_in_prism_2d(c["pos"][0], c["pos"][1], self.target_roof["prism"]):
                    roof_loot = c["pos"]
                    break
            if roof_loot is None and empty_slot is not None:
                for p in pickups:
                    if p["kind"] not in held_kinds and abs(p["pos"][2] - pos[2]) < 3.0 and dist2d(pos, p["pos"]) < 18.0 and point_in_prism_2d(p["pos"][0], p["pos"][1], self.target_roof["prism"]):
                        roof_loot = p["pos"]
                        break

            if self.regen_mode:
                # Retreat to roof center to break LOS from ground enemies & heal at 45 HP/s!
                if d_center > 3.0:
                    move_x, move_y = rcx - pos[0], rcy - pos[1]
                    want_boost = enemies_with_los > 0 and energy > 20.0
            elif roof_loot is not None and enemies_with_los == 0:
                move_x, move_y = roof_loot[0] - pos[0], roof_loot[1] - pos[1]
                silent_walk = True
            elif target is not None:
                # Stay safely on the roof: strafe if we have LOS, or stay near center
                ex, ey = target["pos"][0], target["pos"][1]
                dx, dy = ex - pos[0], ey - pos[1]
                d = max(math.hypot(dx, dy), 1e-4)
                if target_los:
                    # Gentle strafe that stays inside the roof polygon
                    sx, sy = (-dy / d) * self.strafe_dir, (dx / d) * self.strafe_dir
                    if point_in_prism_2d(pos[0] + sx * 4.0, pos[1] + sy * 4.0, self.target_roof["prism"]):
                        move_x, move_y = sx, sy
                    else:
                        self.strafe_dir = -self.strafe_dir
                        move_x, move_y = rcx - pos[0], rcy - pos[1]
                    if target.get("charging"):
                        want_boost = energy > 20.0
                else:
                    # Inch toward enemy only if still safely 3.5m inside roof
                    nx, ny = dx / d, dy / d
                    if point_in_prism_2d(pos[0] + nx * 4.0, pos[1] + ny * 4.0, self.target_roof["prism"]):
                        move_x, move_y = nx, ny
                        silent_walk = True
            else:
                # Pure silent camp at roof center
                if d_center > 4.0:
                    move_x, move_y = rcx - pos[0], rcy - pos[1]
                    silent_walk = True
        elif self.target_roof is not None:
            # Move toward target rooftop and jet up when we reach its wall
            rcx, rcy = self.target_roof["cx"], self.target_roof["cy"]
            dx, dy = rcx - pos[0], rcy - pos[1]
            d = max(math.hypot(dx, dy), 1e-4)
            nx, ny = dx / d, dy / d

            # Check if the target roof's wall is right in front of us
            hitting_target_wall = point_in_prism_2d(pos[0] + nx * 4.5, pos[1] + ny * 4.5, self.target_roof["prism"])
            if hitting_target_wall and pos[2] < self.target_roof["top"] - 0.5:
                if energy >= 45.0 or self.climbing:
                    self.climbing = True
                    move_x, move_y = nx, ny
                    if me["grounded"]:
                        want_jump = True
                    else:
                        want_boost = energy > 5.0
                else:
                    # Wait briefly at base of wall to recharge energy before jetting up
                    self.climbing = False
                    move_x, move_y = 0.0, 0.0
            else:
                self.climbing = False
                move_x, move_y = nx, ny
                # Walk silently if no enemy is shooting at us
                if enemies_with_los == 0 and d_zone < zr - 15.0:
                    silent_walk = True
                elif enemies_with_los > 0:
                    want_boost = energy > 30.0
        else:
            # Fallback if no roof in tiny final circle: hold near zone center silently
            if d_zone > 8.0:
                move_x, move_y = zc[0] - pos[0], zc[1] - pos[1]
                silent_walk = enemies_with_los == 0

        # Apply wall steering (unless actively climbing our target roof)
        if self.climbing:
            mag = max(math.hypot(move_x, move_y), 1e-4)
            mx, my = move_x / mag, move_y / mag
        else:
            mx, my = self.map.steer_around_walls(pos, move_x, move_y)
            if silent_walk:
                # 0.40 * 11.7 m/s = 4.68 m/s < 5.0 m/s -> 100% SILENT!
                mx *= 0.40
                my *= 0.40

        action["move"] = [round(mx, 4), round(my, 4)]
        action["boost"] = want_boost
        action["jump"] = want_jump

        if not self.climbing and self.stuck_ticks >= 5 and (abs(mx) > 0.1 or abs(my) > 0.1):
            if me["grounded"]:
                action["jump"] = True
            elif energy > 25.0:
                action["boost"] = True
            if self.stuck_ticks >= 12:
                self.strafe_dir = -self.strafe_dir
                self.stuck_ticks = 0

        # 6. Aiming & Firing
        if target is not None:
            tpos = target["pos"]
            tvel = target.get("vel", [0.0, 0.0, 0.0])
            aim_center = (tpos[0], tpos[1], tpos[2] + 4.5)
            d_target = dist3d(muzzle_pos, aim_center)

            gun_speed = 252.0
            for w in weapons:
                if w and w["kind"] == "gun":
                    gun_speed = w["stats"].get("speed", 252.0)

            lead_pt = self.lead_target(muzzle_pos, aim_center, tvel, gun_speed)
            des_yaw, des_pitch = self.aim_at_point(muzzle_pos, lead_pt)
            yaw_err = abs(angle_diff(des_yaw, cur_yaw))

            # Hold fire in regen_mode if enemy cannot see us (lets 45 HP/s regen tick!)
            hold_fire = self.regen_mode and enemies_with_los == 0

            fire_flags = [False, False]
            if not hold_fire:
                for s_idx, w in enumerate(weapons):
                    if w is None:
                        continue
                    kind = w["kind"]
                    wstats = w.get("stats", {})

                    if kind == "lightning":
                        if target_los and d_target <= wstats.get("range", 45.0) - 1.0 and yaw_err < 0.48 and energy > 8.0:
                            fire_flags[s_idx] = True

                    elif kind == "gun":
                        if target_los and d_target <= wstats.get("range", 230.0) and yaw_err < 0.16 and w.get("reload", 0.0) <= 0.0:
                            fire_flags[s_idx] = True

                    elif kind == "laser":
                        charge = w.get("charge", 0.0)
                        full_charge = wstats.get("charge", 0.75)
                        min_charge = wstats.get("min_charge", 0.15)
                        if target_los and d_target <= wstats.get("range", 280.0):
                            des_yaw, des_pitch = self.aim_at_point(muzzle_pos, aim_center)
                            yaw_err = abs(angle_diff(des_yaw, cur_yaw))
                            if charge == 0.0:
                                if w.get("cooldown", 0.0) <= 0.0 and yaw_err < 0.45:
                                    fire_flags[s_idx] = True
                            else:
                                if (charge >= full_charge * 0.75 and yaw_err < 0.08) or (
                                    charge >= min_charge and target.get("hp", 1000) < 160 and yaw_err < 0.09
                                ):
                                    fire_flags[s_idx] = False
                                else:
                                    fire_flags[s_idx] = True

                    elif kind == "mortar":
                        if d_target <= 215.0 and d_target >= 18.0 and w.get("cooldown", 0.0) <= 0.0:
                            des_pitch = self.mortar_pitch(muzzle_pos, aim_center, wstats.get("speed", 72.0), wstats.get("gravity", 22.0))
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
                action["aim"] = [round(wrap_angle(cur_yaw + 0.3), 4), 0.0]

            for w in weapons:
                if w and w["kind"] == "gun" and w.get("reload", 0.0) <= 0.0:
                    mag = w["stats"].get("mag", 12)
                    if w.get("ammo", mag) < mag:
                        action["reload"] = True

        mode = "ROOF-REGEN" if (on_roof and self.regen_mode) else ("ROOF-CAMP" if on_roof else ("CLIMB" if self.climbing else "ROTATE"))
        mf.draw.label(f"v4 | hp {me['hp']:.0f} | {mode}")
        return action


if __name__ == "__main__":
    mf.run(Bot)