import copy
import json
import os
import tempfile
import unittest

from factory.core import ProductionLine


class ProductionLineTest(unittest.TestCase):
    def setUp(self):
        self.game = ProductionLine()
        self.game.stock("steel", 10)
        self.game.stock("chip", 10)
        self.game.define_product("arm", {"steel": 2, "chip": 1}, 2)

    def _archive_path(self):
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    # 1. A finished job must free the workstation and its locked parts.
    def test_station_released_after_completion(self):
        self.game.place_order("o1", "arm")
        self.assertEqual(self.game.assign("o1", "W1"), "started")
        self.game.tick()
        self.assertEqual(self.game.tick(), ["o1"])
        station = self.game.stations["W1"]
        self.assertIsNone(station["order"])
        self.assertEqual(station["locked"], {})
        self.assertEqual(station["remaining"], 0)
        self.assertEqual(self.game.orders["o1"]["status"], "done")
        self.assertEqual(self.game.completed, ["o1"])

    # 2. Parallel jobs sharing a material deduct stock exactly once each.
    def test_parallel_jobs_deduct_shared_material_once(self):
        self.game.place_order("o1", "arm")
        self.game.place_order("o2", "arm")
        self.assertEqual(self.game.assign("o1", "W1"), "started")
        self.assertEqual(self.game.assign("o2", "W2"), "started")
        self.assertEqual(self.game.materials["steel"], 6)
        self.assertEqual(self.game.materials["chip"], 8)
        self.assertEqual(self.game.stations["W1"]["locked"], {"steel": 2, "chip": 1})
        self.assertEqual(self.game.stations["W2"]["locked"], {"steel": 2, "chip": 1})

    # 3. A breakdown freezes the job timer until the station is back up.
    def test_breakdown_freezes_job_timer(self):
        self.game.place_order("o1", "arm")
        self.game.assign("o1", "W1")
        self.game.tick()
        self.assertEqual(self.game.stations["W1"]["remaining"], 1)
        self.game.breakdown("W1", duration=2)
        self.game.tick()
        self.game.tick()
        self.assertEqual(self.game.stations["W1"]["remaining"], 1)
        self.assertEqual(self.game.completed, [])
        self.assertEqual(self.game.tick(), ["o1"])
        self.assertEqual(self.game.time, 4)

    # 4. Rework reuses the existing order instead of opening a new one.
    def test_rework_does_not_create_new_order(self):
        self.game.place_order("o1", "arm")
        self.game.assign("o1", "W1")
        self.game.tick()
        self.game.tick()
        self.assertEqual(self.game.rework("o1"), "rework_queued")
        self.assertEqual(list(self.game.orders), ["o1"])
        order = self.game.orders["o1"]
        self.assertTrue(order["rework"])
        self.assertEqual(order["status"], "pending")
        self.assertEqual(self.game.completed, [])
        self.assertEqual(self.game.assign("o1", "W2"), "started")

    # 5. Quality inspection must never mutate the product template.
    def test_inspection_leaves_template_untouched(self):
        before = copy.deepcopy(self.game.products["arm"])
        self.assertEqual(self.game.inspect("arm"), "pass")
        self.assertEqual(self.game.inspect("arm"), "pass")
        self.assertEqual(self.game.products["arm"], before)
        self.assertEqual(self.game.products["arm"]["materials"], {"steel": 2, "chip": 1})
        self.assertEqual(self.game.products["arm"]["duration"], 2)

    # 6. Cancelling an order returns its locked materials to inventory.
    def test_cancel_returns_locked_materials(self):
        self.game.place_order("o1", "arm")
        self.game.assign("o1", "W1")
        self.assertEqual(self.game.materials["steel"], 8)
        self.assertEqual(self.game.materials["chip"], 9)
        self.assertEqual(self.game.cancel("o1"), "cancelled")
        self.assertEqual(self.game.materials["steel"], 10)
        self.assertEqual(self.game.materials["chip"], 10)
        station = self.game.stations["W1"]
        self.assertIsNone(station["order"])
        self.assertEqual(station["locked"], {})
        self.assertEqual(station["remaining"], 0)
        self.assertEqual(self.game.orders["o1"]["status"], "cancelled")

    # 7. Completed maintenance resets the fault counter to zero.
    def test_maintenance_resets_fault_counter(self):
        self.game.breakdown("W1")
        self.game.breakdown("W2")
        self.assertEqual(self.game.faults, 2)
        self.assertEqual(self.game.maintain(duration=2), "started")
        self.game.tick()
        self.assertEqual(self.game.faults, 2)
        self.game.tick()
        self.assertIsNone(self.game.maintenance)
        self.assertEqual(self.game.faults, 0)

    # 8. A restored archive continues the clock without skipping a tick.
    def test_load_does_not_skip_tick(self):
        path = self._archive_path()
        self.game.place_order("o1", "arm")
        self.game.assign("o1", "W1")
        self.game.tick()
        self.game.save(path)
        restored = ProductionLine().load(path)
        self.assertEqual(restored.time, 1)
        self.assertEqual(restored.stations["W1"]["remaining"], 1)
        self.assertEqual(restored.tick(), ["o1"])
        self.assertEqual(restored.time, 2)
        self.assertEqual(restored.completed, ["o1"])
        self.assertIsNone(restored.stations["W1"]["order"])

    # Contention: two stations race for the last units of a material.
    def test_parallel_contention_for_last_material(self):
        game = ProductionLine()
        game.stock("steel", 3)
        game.define_product("arm", {"steel": 2}, 1)
        game.place_order("o1", "arm")
        game.place_order("o2", "arm")
        self.assertEqual(game.assign("o1", "W1"), "started")
        self.assertEqual(game.assign("o2", "W2"), "no_material")
        self.assertEqual(game.materials["steel"], 1)
        self.assertIsNone(game.stations["W2"]["order"])
        self.assertEqual(game.stations["W2"]["locked"], {})
        self.assertEqual(game.orders["o2"]["status"], "pending")

    # Boundary: with zero stock nothing is locked and the station stays free.
    def test_zero_inventory_boundary(self):
        game = ProductionLine()
        game.define_product("arm", {"steel": 2}, 1)
        game.place_order("o1", "arm")
        self.assertEqual(game.assign("o1", "W1"), "no_material")
        self.assertEqual(game.materials.get("steel", 0), 0)
        station = game.stations["W1"]
        self.assertIsNone(station["order"])
        self.assertEqual(station["locked"], {})
        self.assertEqual(game.orders["o1"]["status"], "pending")
        self.assertEqual(game.tick(), [])
        self.assertEqual(game.time, 1)

    # Boundary: stock exactly equal to the requirement drains to zero.
    def test_exact_stock_boundary(self):
        game = ProductionLine()
        game.stock("steel", 2)
        game.define_product("arm", {"steel": 2}, 1)
        game.place_order("o1", "arm")
        self.assertEqual(game.assign("o1", "W1"), "started")
        self.assertEqual(game.materials["steel"], 0)
        self.assertEqual(game.tick(), ["o1"])
        self.assertEqual(game.materials["steel"], 0)

    # Archive round trip keeps stations, materials, timers and orders consistent.
    def test_archive_round_trip_keeps_consistent_state(self):
        path = self._archive_path()
        self.game.place_order("o1", "arm")
        self.game.assign("o1", "W1")
        self.game.breakdown("W2", duration=3)
        self.game.maintain(duration=2)
        self.game.tick()
        self.game.save(path)
        with open(path, "r", encoding="utf-8") as handle:
            archive = json.load(handle)
        self.assertEqual(archive["time"], 1)
        restored = ProductionLine().load(path)
        self.assertEqual(restored.time, self.game.time)
        self.assertEqual(restored.materials, self.game.materials)
        self.assertEqual(restored.products, self.game.products)
        self.assertEqual(restored.orders, self.game.orders)
        self.assertEqual(restored.stations, self.game.stations)
        self.assertEqual(restored.faults, self.game.faults)
        self.assertEqual(restored.maintenance, self.game.maintenance)
        self.assertEqual(restored.completed, self.game.completed)

    # Saved state must include the random event sequence.
    def test_save_preserves_event_sequence(self):
        path = self._archive_path()
        reference = ProductionLine()
        expected = [self.game.next_event() for _ in range(3)]
        for _ in range(3):
            reference.next_event()
        self.game.save(path)
        with open(path, "r", encoding="utf-8") as handle:
            archive = json.load(handle)
        self.assertEqual(archive["events"], expected)
        restored = ProductionLine().load(path)
        self.assertEqual(restored.events, expected)
        self.assertEqual(restored.next_event(), reference.next_event())

    # The CMD menu dispatches every instruction through the same API.
    def test_cmd_menu_dispatch(self):
        game = ProductionLine()
        self.assertEqual(game.cmd("stock steel 4"), "ok")
        self.assertEqual(game.cmd("product arm 1 steel 2"), "ok")
        self.assertEqual(game.cmd("order o1 arm"), "queued")
        self.assertEqual(game.cmd("assign o1 W1"), "started")
        self.assertEqual(game.cmd("tick"), ["o1"])
        self.assertEqual(game.cmd("rework o1"), "rework_queued")
        self.assertEqual(game.cmd("cancel o1"), "cancelled")
        self.assertEqual(game.materials["steel"], 2)
        with self.assertRaises(ValueError):
            game.cmd("explode")


if __name__ == "__main__":
    unittest.main()
