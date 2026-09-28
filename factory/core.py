"""Offline robot production line game core.

Fixed seed, logical clock, CMD instruction dispatch, JSON save/load archives.
"""

import copy
import json
import random

DEFAULT_SEED = 56
DEFAULT_STATIONS = ("W1", "W2")


def _rng_state_to_json(state):
    version, internal, gauss = state
    return {"version": version, "internal": list(internal), "gauss": gauss}


def _rng_state_from_json(data):
    return (data["version"], tuple(data["internal"]), data["gauss"])


def _new_station():
    return {"order": None, "remaining": 0, "locked": {}, "down": 0}


class ProductionLine:
    def __init__(self, seed=DEFAULT_SEED, stations=DEFAULT_STATIONS):
        self.seed = seed
        self.time = 0
        self.materials = {}
        self.products = {}
        self.orders = {}
        self.stations = {name: _new_station() for name in stations}
        self.faults = 0
        self.maintenance = None
        self.completed = []
        self.events = []
        self.audit = []
        self._rng = random.Random(seed)

    # -- random event sequence ------------------------------------------
    def next_event(self):
        value = self._rng.randrange(1, 1000000)
        self.events.append(value)
        return value

    # -- inventory --------------------------------------------------------
    def stock(self, name, qty):
        self.materials[name] = self.materials.get(name, 0) + qty
        self.audit.append(["stock", name, qty, self.time])

    def define_product(self, name, materials, duration):
        self.products[name] = {"materials": dict(materials), "duration": duration}
        self.audit.append(["product", name, self.time])

    # -- orders -------------------------------------------------------------
    def place_order(self, order_id, product):
        if product not in self.products:
            self.audit.append(["unknown_product", order_id, self.time])
            return "unknown_product"
        if order_id in self.orders:
            self.audit.append(["duplicate_order", order_id, self.time])
            return "duplicate"
        self.orders[order_id] = {
            "id": order_id,
            "product": product,
            "status": "pending",
            "rework": False,
        }
        self.audit.append(["order", order_id, self.time])
        return "queued"

    def _free_station(self, station, refund):
        """Return a station to idle.

        When ``refund`` is true every locked part goes back to inventory
        (cancellation); otherwise the parts are treated as consumed by a
        finished job and simply unlocked.
        """
        if refund:
            for material, qty in station["locked"].items():
                self.materials[material] = self.materials.get(material, 0) + qty
        station["order"] = None
        station["remaining"] = 0
        station["locked"] = {}

    def assign(self, order_id, station_name):
        order = self.orders.get(order_id)
        if order is None or order["status"] != "pending":
            self.audit.append(["not_pending", order_id, self.time])
            return "not_pending"
        station = self.stations[station_name]
        if station["order"] is not None:
            self.audit.append(["station_busy", station_name, self.time])
            return "busy"
        if station["down"] > 0:
            self.audit.append(["station_down", station_name, self.time])
            return "down"
        required = self.products[order["product"]]["materials"]
        # Validate the whole bill of materials before deducting anything so
        # parallel assignments sharing a material never double-charge it and
        # a failed assignment leaves inventory and the station untouched.
        for material, qty in required.items():
            if self.materials.get(material, 0) < qty:
                self.audit.append(["no_material", order_id, self.time])
                return "no_material"
        locked = {}
        for material, qty in required.items():
            self.materials[material] -= qty
            locked[material] = qty
        station["order"] = order_id
        station["remaining"] = self.products[order["product"]]["duration"]
        station["locked"] = locked
        order["status"] = "building"
        self.audit.append(["started", order_id, station_name, self.time])
        return "started"

    def cancel(self, order_id):
        order = self.orders.get(order_id)
        if order is None:
            return "unknown"
        if order["status"] in ("done", "cancelled"):
            return order["status"]
        if order["status"] == "building":
            for station in self.stations.values():
                if station["order"] == order_id:
                    self._free_station(station, refund=True)
                    break
        order["status"] = "cancelled"
        self.audit.append(["cancelled", order_id, self.time])
        return "cancelled"

    def rework(self, order_id):
        order = self.orders.get(order_id)
        if order is None:
            return "unknown"
        if order["status"] != "done":
            self.audit.append(["rework_rejected", order_id, self.time])
            return "not_done"
        # Rework reopens the same order; no new order id is ever created.
        order["status"] = "pending"
        order["rework"] = True
        if order_id in self.completed:
            self.completed.remove(order_id)
        self.audit.append(["rework", order_id, self.time])
        return "rework_queued"

    # -- quality inspection: never mutates the product template ------------
    def inspect(self, product):
        template = self.products.get(product)
        if template is None:
            return "unknown"
        sample = copy.deepcopy(template)
        sample["duration"] = max(1, sample["duration"] - 1)
        for material, qty in sample["materials"].items():
            if self.materials.get(material, 0) < qty:
                self.audit.append(["inspect_fail", product, self.time])
                return "fail"
        self.audit.append(["inspect_pass", product, self.time])
        return "pass"

    # -- breakdowns and maintenance -----------------------------------------
    def breakdown(self, station_name, duration=1):
        station = self.stations[station_name]
        station["down"] = max(station["down"], duration)
        self.faults += 1
        self.audit.append(["breakdown", station_name, self.time])
        return "down"

    def maintain(self, duration=1):
        if self.maintenance is not None:
            self.audit.append(["maintenance_busy", self.time])
            return "busy"
        self.maintenance = {"remaining": duration}
        self.audit.append(["maintenance_started", self.time])
        return "started"

    # -- logical clock ---------------------------------------------------------
    def tick(self):
        self.time += 1
        if self.maintenance is not None:
            self.maintenance["remaining"] -= 1
            if self.maintenance["remaining"] <= 0:
                self.maintenance = None
                self.faults = 0
                self.audit.append(["maintenance_done", self.time])
        finished = []
        for name, station in self.stations.items():
            if station["down"] > 0:
                # A broken station is frozen: downtime counts down but the
                # locked job timer does not advance and no parts are touched.
                station["down"] -= 1
                self.audit.append(["frozen", name, self.time])
                continue
            if station["order"] is None:
                continue
            station["remaining"] -= 1
            if station["remaining"] <= 0:
                order_id = station["order"]
                # Completed parts are consumed: clear the lock without a
                # refund and move station and order to a consistent state.
                self._free_station(station, refund=False)
                self.orders[order_id]["status"] = "done"
                self.completed.append(order_id)
                finished.append(order_id)
                self.audit.append(["completed", order_id, self.time])
        return finished

    # -- CMD instruction dispatch -----------------------------------------
    def cmd(self, line):
        parts = line.split()
        name = parts[0]
        args = parts[1:]
        if name == "tick":
            return self.tick()
        if name == "stock":
            self.stock(args[0], int(args[1]))
            return "ok"
        if name == "product":
            materials = {}
            for index in range(2, len(args), 2):
                materials[args[index]] = int(args[index + 1])
            self.define_product(args[0], materials, int(args[1]))
            return "ok"
        if name == "order":
            return self.place_order(args[0], args[1])
        if name == "assign":
            return self.assign(args[0], args[1])
        if name == "cancel":
            return self.cancel(args[0])
        if name == "rework":
            return self.rework(args[0])
        if name == "inspect":
            return self.inspect(args[0])
        if name == "breakdown":
            duration = int(args[1]) if len(args) > 1 else 1
            return self.breakdown(args[0], duration)
        if name == "maintain":
            duration = int(args[0]) if args else 1
            return self.maintain(duration)
        if name == "event":
            return str(self.next_event())
        if name == "save":
            self.save(args[0])
            return "saved"
        if name == "load":
            self.load(args[0])
            return "loaded"
        raise ValueError("unknown command: %s" % name)

    # -- JSON archive -------------------------------------------------------
    def save(self, path):
        state = {
            "seed": self.seed,
            "time": self.time,
            "materials": dict(self.materials),
            "products": copy.deepcopy(self.products),
            "orders": copy.deepcopy(self.orders),
            "stations": copy.deepcopy(self.stations),
            "faults": self.faults,
            "maintenance": copy.deepcopy(self.maintenance),
            "completed": list(self.completed),
            "events": list(self.events),
            "audit": [list(event) for event in self.audit],
            "rng_state": _rng_state_to_json(self._rng.getstate()),
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)

    def load(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        self.seed = state["seed"]
        self.time = state["time"]
        self.materials = dict(state["materials"])
        self.products = copy.deepcopy(state["products"])
        self.orders = copy.deepcopy(state["orders"])
        self.stations = copy.deepcopy(state["stations"])
        self.faults = state["faults"]
        self.maintenance = copy.deepcopy(state["maintenance"])
        self.completed = list(state["completed"])
        self.events = list(state["events"])
        self.audit = [list(event) for event in state["audit"]]
        self._rng = random.Random(self.seed)
        self._rng.setstate(_rng_state_from_json(state["rng_state"]))
        return self
