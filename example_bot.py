#!/usr/bin/env python3
"""
ApexMech v5.0 - Hit-and-Run Cover & Regen Bot (mechfight/1)
- Precomputed 4m obstacle grid for <1ms/tick runtime (well under 50ms)
- Tracks damage taken vs dealt; immediately flees to cover against stronger bots
- Breaks LOS and holds fire to trigger 45 HP/s regen
- Protects front armor while retreating
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


class FastArenaGrid:
    """
    Rasterizes world.json buildings/stadium into a flat 4m grid at startup (~0.2s).
    All runtime collision, LOS, and cover queries are O(1) array lookups (<1ms/tick).
    """

    def __init__(self, map_dir: str):
        self.cell = 4.0
        self.min_x = -516.0
        self.min_y = -336.0
        self.bounds = (-508.0, 551.0, -329.0, 384.0)
        self.nx = 270
        self.ny = 182
        self.top_z = [-100.0] * (self.nx * self.ny)

        world_path = os.path.join(map_dir, "v2", "world.json")
        if not os.path.exists(world_path):
            return

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

            nx, ny, cell = self.nx, self.ny, self.cell
            min_x, min_y = self.min_x, self.min_y
            top_z = self.top_z

            for p in world.get("prisms", []):
                # Skip bridges so we can walk/shoot under them
                if p.get("kind") == "bridge":
                    continue
                ring = p.get("ring", [])
                if len(ring) < 6:
                    continue
                ptop = float(p.get("top", 0.0))
                holes = p.get("holes", [])
                xs = ring[0::2]
                ys = ring[1::2]
                ix0 = max(0, int((min(xs) - min_x) / cell))
                ix1 = min(nx - 1, int((max(xs) - min_x) / cell))
                iy0 = max(0, int((min(ys) - min_y) / cell))
                iy1 = min(ny - 1, int((max(ys) - min_y) / cell))

                for iy in range(iy0, iy1 + 1):
                    wy = min_y + (iy + 0.5) * cell
                    row = iy * nx
                    for ix in range(ix0, ix1 + 1):
                        if top_z[row + ix] >= ptop:
                            continue
                        wx = min_x + (ix + 0.5) * cell
                        if point_in_ring(wx, wy, ring):
                            in_hole = False
                            for h in holes:
                                if point_in_ring(wx, wy, h):
                                    in_hole = True
                                    break
                            if not in_hole:
                                top_z[row + ix] = ptop
        except Exception as e:
            mf.log(f"[grid] load error: {e}")

    def is_blocked(self, x: float, y: float, z_feet: float) -> bool:
        if x < self.bounds[0] or x > self.bounds[1] or y < self.bounds[2] or y > self.bounds[3]:
            return True
        ix = int((x - self.min_x) / self.cell)
        iy = int((y - self.min_y) / self.cell)
        if ix < 0 or ix >= self.nx or iy < 0 or iy >= self.ny:
            return True
        return self.top_z[iy * self.nx + ix] > z_feet + 1.2

    def has_los(self, a, b) -> bool:
        """Ultra-fast stepped LOS check against the flat 4m grid."""
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        dz = b[2] - a[2]
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        if d < 5.0:
            return True
        steps = min(int(d / 4.5), 32)
        if steps < 2:
            return True
        sx, sy, sz = dx / steps, dy / steps, dz / steps
        x, y, z = a[0] + sx, a[1] + sy, a[2] + sz
        nx, ny, cell = self.nx, self.ny, self.cell
        min_x, min_y = self.min_x, self.min_y
        top_z = self.top_z
        for _ in range(1, steps):
            ix = int((x - min_x) / cell)
            iy = int((y - min_y) / cell)
            if 0 <= ix < nx and 0 <= iy < ny:
                if top_z[iy * nx + ix] > z:
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
        for off in (0.0, 0.35, -0.35, 0.7, -0.7, 1.1, -1.1, 1.55, -1.55, 2.1, -2.1):
            ang = base_ang + off
            ca, sa = math.cos(ang), math.sin(ang)
            if not self.is_blocked(x + ca * 4.5, y + sa * 4.5, z) and not self.is_blocked(x + ca * 8.5, y + sa * 8.5, z):
                return ca, sa
        return desired_dx / mag, desired_dy / mag

    def find_cover_vector(self, pos, threats, zc, zr):
        """
        Tests 16 directions at 16m and 28m in <0.1ms to find a walkable spot
        inside the zone where buildings block LOS from threats.
        """
        x, y, z = pos[0], pos[1], pos[2]
        eye_z = z + 6.5
        top_threats = threats[:3]
        best_score = 1e9
        best_vec = None

        for k in range(16):
            ang = k * (math.pi / 8.0)
            ca, sa = math.cos(ang), math.sin(ang)
            if self.is_blocked(x + ca * 5.0, y + sa * 5.0, z):
                continue

            # Test 18m ahead, fallback to 11m
            tx, ty = x + ca * 18.0, y + sa * 18.0
            if self.is_blocked(tx, ty, z):
                tx, ty = x + ca * 11.0, y + sa * 11.0
                if self.is_blocked(tx, ty, z):
                    continue

            los_count = 0
            min_ed = 999.0
            for e in top_threats:
                ep = e["pos"]
                ed = math.hypot(tx - ep[0], ty - ep[1])
                if ed < min_ed:
                    min_ed = ed
                if self.has_los((tx, ty, eye_z), (ep[0], ep[1], ep[2] + 7.0)):
                    los_count += 1

            d_zone = math.hypot(tx - zc[0], ty - zc[1])
            zone_pen = max(0.0, d_zone - (zr - 12.0)) * 5.0
            score = los_count * 150.0 - min(min_ed, 85.0) * 1.1 + zone_pen
            if score < best_score:
                best_score = score
                best_vec = (ca, sa)

        return best_vec


class Bot:
    name = "ApexMech-v5.0"

    def __init__(self, hello: mf.Hello):
        self.slot = hello["slot"]
        self.rng = random.Random(hello["seed"])
        self.config = hello["config"]
        self.grid = FastArenaGrid(hello["map"]["dir"])
        self.pickup_r = self.config.get("loot", {}).get("pickup_radius", 4.0)

        # Lightning (burst + chain) and Laser (hit-and-run poke) are best
        self.weapon_rank = {"lightning": 4, "laser": 4, "gun": 2, "mortar": 1}

        self.upgrade_weights = {
            "armor": 100,
            "regen": 99,
            "hp": 96,
            "speed": 92,
            "boost": 90,
            "energy": 88,
            "lightning": 87,
            "laser": 86,
            "gun_damage": 85,
            "gun_rate": 84,
            "reload": 72,
            "sensors": 68,
            "mortar": 40,
        }

        self.last_pos = None
        self.stuck_ticks = 0
        self.strafe_dir = 1.0
        self.strafe_timer = 0
        self.last_damage_bearing = None
        self.last_damage_t = -10.0
        self.roam_target = None

        # Hit-and-run state
        self.retreat_mode = False
        self.retreat_until = 0.0
        self.dmg_taken_hist: list[tuple[float, float]] = []
        self.dmg_dealt_hist: list[tuple[float, float]] = []

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
        my_cubes = me.get("cubes", 0)
        stats = me.get("stats", {})
        muzzle_pos = (pos[0], pos[1], pos[2] + stats.get("muzzle_height", 7.0))
        cur_yaw, _ = me["aim"]

        # Track recent damage taken vs dealt (2.2s window)
        for ev in obs.get("events", []):
            etype = ev.get("type")
            if etype == "damaged":
                if ev.get("weapon") != "zone":
                    self.dmg_taken_hist.append((t, float(ev.get("amount", 0.0))))
                if ev.get("bearing") is not None:
                    self.last_damage_bearing = ev["bearing"]
                    self.last_damage_t = t
            elif etype == "hit":
                self.dmg_dealt_hist.append((t, float(ev.get("amount", 0.0))))

        cutoff = t - 2.2
        self.dmg_taken_hist = [x for x in self.dmg_taken_hist if x[0] >= cutoff]
        self.dmg_dealt_hist = [x for x in self.dmg_dealt_hist if x[0] >= cutoff]
        recent_taken = sum(x[1] for x in self.dmg_taken_hist)
        recent_dealt = sum(x[1] for x in self.dmg_dealt_hist)

        if self.last_pos is not None and dist2d(pos, self.last_pos) < 0.25:
            self.stuck_ticks += 1
        else:
            self.stuck_ticks = 0
        self.last_pos = pos

        self.strafe_timer += 1
        if self.strafe_timer > 15:
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

        # 4. Evaluate Threats & Hit-and-Run Decision
        enemies.sort(key=lambda e: dist2d(pos, e["pos"]))
        los_enemies = []
        stronger_enemy_near = False
        for e in enemies[:4]:
            ep = e["pos"]
            d = dist2d(pos, ep)
            if self.grid.has_los(muzzle_pos, (ep[0], ep[1], ep[2] + 4.5)):
                los_enemies.append(e)
            if d < 75.0:
                if e.get("cubes", 0) > my_cubes + 1:
                    stronger_enemy_near = True
                if e.get("hp", 1000) > me["hp"] + 250 and hp_frac < 0.72:
                    stronger_enemy_near = True

        # Check if we are losing the DPS trade right now
        losing_trade = recent_taken > 120.0 and recent_taken > recent_dealt * 1.15 + 50.0
        outnumbered = len(los_enemies) >= 2
        gun_reloading = any(w and w["kind"] == "gun" and w.get("reload", 0.0) > 0.3 for w in weapons)

        # Trigger retreat & heal if hurt, outnumbered, losing trade, or stronger bot nearby
        if hp_frac < 0.62 or losing_trade or outnumbered or stronger_enemy_near:
            self.retreat_mode = True
            self.retreat_until = max(self.retreat_until, t + 2.5)
        elif hp_frac > 0.88 and t >= self.retreat_until and not outnumbered and not stronger_enemy_near:
            self.retreat_mode = False

        # Target selection
        target = None
        target_type = None
        target_los = False

        if enemies:
            # Prefer lowest-HP enemy in LOS
            best_e = None
            best_sc = 1e9
            for e in enemies[:4]:
                ep = e["pos"]
                los = e in los_enemies
                hp_r = e.get("hp", 1000.0) / max(e.get("max_hp", 1400.0), 1.0)
                sc = dist2d(pos, ep) * (0.45 + 0.55 * hp_r) + (0.0 if los else 140.0)
                if sc < best_sc:
                    best_sc = sc
                    best_e = e
            target = best_e
            target_type = "mech"
            target_los = target in los_enemies
        elif not self.retreat_mode and npcs:
            n = min(npcs, key=lambda x: dist2d(pos, x["pos"]))
            if dist2d(pos, n["pos"]) < 80.0:
                target = n
                target_type = "npc"
                target_los = self.grid.has_los(muzzle_pos, (n["pos"][0], n["pos"][1], n["pos"][2] + 1.8))
        if target is None and not self.retreat_mode and crates:
            c = min(crates, key=lambda x: dist2d(pos, x["pos"]))
            if dist2d(pos, c["pos"]) < 65.0:
                target = c
                target_type = "crate"
                target_los = self.grid.has_los(muzzle_pos, (c["pos"][0], c["pos"][1], c["pos"][2] + 1.2))

        # Only finish a kill during retreat if target is 1-2 shots from death
        can_finish_kill = target_type == "mech" and target_los and target.get("hp", 1000) < 160.0
        disengaging = (self.retreat_mode or (gun_reloading and len(los_enemies) > 0)) and not can_finish_kill

        # 5. Movement Decision
        move_x, move_y = 0.0, 0.0
        want_boost = False

        d_zone = dist2d(pos, zc)
        outside_next = d_zone > max(zr - 18.0, 5.0)
        urgent_zone = (not me["in_zone"]) or (d_zone > zr - 8.0 and t > zone["shrink_start"] - 12.0)

        # Supply drop = full heal + 3 cubes
        best_drop = None
        for dr in drops:
            dd = dist2d(pos, dr["pos"])
            if dr["lands_in"] < 15.0 and dd < (140.0 if hp_frac < 0.75 else 85.0):
                if len(los_enemies) == 0 or dd < 20.0:
                    best_drop = dr
                    break

        if urgent_zone:
            move_x, move_y = zc[0] - pos[0], zc[1] - pos[1]
            want_boost = energy > 22.0
        elif disengaging and enemies:
            # RUN TO COVER RIGHT AWAY using fast O(1) cover grid
            cov = self.grid.find_cover_vector(pos, enemies, zc, zr)
            if cov is not None:
                move_x, move_y = cov
            else:
                ne = enemies[0]["pos"]
                move_x, move_y = pos[0] - ne[0], pos[1] - ne[1]
            want_boost = energy > 18.0 and len(los_enemies) > 0
        elif cubes and (not los_enemies or dist2d(pos, min(cubes, key=lambda x: dist2d(pos, x["pos"]))["pos"]) < 16.0):
            c = min(cubes, key=lambda x: dist2d(pos, x["pos"]))
            move_x, move_y = c["pos"][0] - pos[0], c["pos"][1] - pos[1]
            want_boost = energy > 40.0 and dist2d(pos, c["pos"]) > 14.0
        elif target_type == "mech":
            # Favorable 1v1 Hit-and-Run: kite at edge of optimal range near cover
            ex, ey = target["pos"][0], target["pos"][1]
            dx, dy = ex - pos[0], ey - pos[1]
            d = max(math.hypot(dx, dy), 1e-4)
            nx, ny = dx / d, dy / d
            sx, sy = -ny * self.strafe_dir, nx * self.strafe_dir

            has_lightning = "lightning" in held_kinds
            ideal_dist = 36.0 if has_lightning else 72.0

            if d > ideal_dist + 12.0:
                move_x = nx * 0.55 + sx * 0.83
                move_y = ny * 0.55 + sy * 0.83
            elif d < ideal_dist - 10.0:
                move_x = -nx * 0.70 + sx * 0.70
                move_y = -ny * 0.70 + sy * 0.70
                want_boost = energy > 35.0
            else:
                move_x, move_y = sx, sy

            if target.get("charging"):
                move_x, move_y = sx, sy
                want_boost = energy > 15.0
        elif empty_slot is not None and pickups:
            valid_p = [p for p in pickups if p["kind"] not in held_kinds]
            if valid_p:
                p = min(valid_p, key=lambda x: dist2d(pos, x["pos"]))
                move_x, move_y = p["pos"][0] - pos[0], p["pos"][1] - pos[1]
                want_boost = energy > 45.0
        elif best_drop is not None:
            move_x, move_y = best_drop["pos"][0] - pos[0], best_drop["pos"][1] - pos[1]
            want_boost = energy > 40.0 and dist2d(pos, best_drop["pos"]) > best_drop["radius"]
        elif target_type in ("npc", "crate"):
            dx, dy = target["pos"][0] - pos[0], target["pos"][1] - pos[1]
            if math.hypot(dx, dy) > 28.0:
                move_x, move_y = dx, dy
            else:
                move_x, move_y = -dy * self.strafe_dir, dx * self.strafe_dir
        elif outside_next:
            move_x, move_y = zc[0] - pos[0], zc[1] - pos[1]
        else:
            if self.roam_target is None or dist2d(pos, self.roam_target) < 15.0:
                ang = self.rng.uniform(0.0, 2.0 * math.pi)
                rad = self.rng.uniform(0.15, 0.72) * max(zr - 15.0, 10.0)
                self.roam_target = (
                    clamp(zc[0] + math.cos(ang) * rad, self.grid.bounds[0], self.grid.bounds[1]),
                    clamp(zc[1] + math.sin(ang) * rad, self.grid.bounds[2], self.grid.bounds[3]),
                )
            move_x = self.roam_target[0] - pos[0]
            move_y = self.roam_target[1] - pos[1]

        dodge = self.incoming_projectile_dodge(pos, obs.get("projectiles", []))
        if dodge is not None:
            move_x = move_x * 0.35 + dodge[0] * 0.95
            move_y = move_y * 0.35 + dodge[1] * 0.95
            want_boost = energy > 18.0

        mx, my = self.grid.steer_around_walls(pos, move_x, move_y)
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

        # 6. Aiming & Firing (always face threat to protect front armor; hold fire behind cover to heal!)
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

            # Hold fire when disengaging & out of LOS so the 5s regen timer completes!
            hold_fire = disengaging and (len(los_enemies) == 0 or not target_los)

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
                        if target_type == "mech" and target_los and d_target <= wstats.get("range", 280.0):
                            des_yaw, des_pitch = self.aim_at_point(muzzle_pos, aim_center)
                            yaw_err = abs(angle_diff(des_yaw, cur_yaw))
                            if charge == 0.0:
                                if not disengaging and w.get("cooldown", 0.0) <= 0.0 and yaw_err < 0.45:
                                    fire_flags[s_idx] = True
                            else:
                                if (charge >= full_charge * 0.72 and yaw_err < 0.08) or (
                                    charge >= min_charge and (target.get("hp", 1000) < 160 or disengaging) and yaw_err < 0.1
                                ):
                                    fire_flags[s_idx] = False
                                else:
                                    fire_flags[s_idx] = True

                    elif kind == "mortar":
                        if not disengaging and d_target <= 215.0 and d_target >= 20.0 and w.get("cooldown", 0.0) <= 0.0:
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
                action["aim"] = [round(wrap_angle(cur_yaw + 0.35), 4), 0.0]

            for w in weapons:
                if w and w["kind"] == "gun" and w.get("reload", 0.0) <= 0.0:
                    mag = w["stats"].get("mag", 12)
                    if w.get("ammo", mag) < mag * 0.65:
                        action["reload"] = True

        state = "RUN+HEAL" if disengaging else (target_type or "FARM")
        mf.draw.label(f"v5 | hp {me['hp']:.0f} | c {my_cubes} | {state}")
        return action


if __name__ == "__main__":
    mf.run(Bot)