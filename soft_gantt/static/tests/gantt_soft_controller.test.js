import { describe, expect, test } from "@odoo/hoot";

import { GanttSoftController } from "@soft_gantt/gantt/gantt_controller";

function makeController() {
    const records = [
        {
            id: 1,
            date_start: "2026-03-02 00:00:00",
            date_deadline: "2026-03-05 00:00:00",
        },
    ];
    const writes = [];
    let releaseFirstWrite;
    const firstWrite = new Promise((resolve) => {
        releaseFirstWrite = resolve;
    });
    const controller = Object.create(GanttSoftController.prototype);
    controller.props = {
        resModel: "project.task",
        archInfo: {
            dateStart: "date_start",
            dateStartType: "datetime",
            dateStop: "date_deadline",
            dateStopType: "datetime",
        },
    };
    controller.model = {
        records,
        context: {},
        reload: async () => {},
    };
    controller.notification = { add: () => {} };
    controller.writeQueues = new Map();
    controller.orm = {
        write: async (resModel, ids, values) => {
            writes.push(values);
            if (writes.length === 1) {
                await firstWrite;
            }
            Object.assign(records[0], values);
        },
    };
    return { controller, writes, releaseFirstWrite };
}

describe("GanttSoftController record writes", () => {
    test("quick reschedules wait for the previous write and use its fresh dates", async () => {
        const { controller, writes, releaseFirstWrite } = makeController();

        const first = controller.onReschedule(1, 1);
        const second = controller.onReschedule(1, 1);
        await Promise.resolve();
        await Promise.resolve();

        expect(writes).toHaveLength(1);
        releaseFirstWrite();
        await Promise.all([first, second]);

        expect(writes).toEqual([
            {
                date_start: "2026-03-03 00:00:00",
                date_deadline: "2026-03-06 00:00:00",
            },
            {
                date_start: "2026-03-04 00:00:00",
                date_deadline: "2026-03-07 00:00:00",
            },
        ]);
        expect(controller.writeQueues.has(1)).toBe(false);
    });

    test("quick resizes are serialized for the same record", async () => {
        const { controller, writes, releaseFirstWrite } = makeController();

        const first = controller.onResize(1, "end", 1);
        const second = controller.onResize(1, "end", 1);
        await Promise.resolve();
        await Promise.resolve();

        expect(writes).toHaveLength(1);
        releaseFirstWrite();
        await Promise.all([first, second]);

        expect(writes).toEqual([
            { date_deadline: "2026-03-06 00:00:00" },
            { date_deadline: "2026-03-07 00:00:00" },
        ]);
    });
});
