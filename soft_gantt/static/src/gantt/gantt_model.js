import { Domain } from "@web/core/domain";
import { Model } from "@web/model/model";
import { getFieldsSpec } from "@web/model/relational_model/utils";
import { orderByToString } from "@web/search/utils/order_by";

import { serializeOdooDate } from "./gantt_utils";

/**
 * Data source of the Gantt view.
 *
 * A Gantt shows every record at once: there is no paging and no grouping, so
 * the full `RelationalModel` machinery would be wasted here. This model turns
 * the current search parameters into one `web_search_read`, plus a count of
 * records outside the timeline because both of their dates are empty.
 *
 * Because the search parameters are read on every `load`, the filters, the
 * favourites and the search bar actually reload the view.
 */
export class GanttSoftModel extends Model {
    setup(params) {
        this.loadId = 0;
        this.resModel = params.resModel;
        this.fields = params.fields;
        this.activeFields = params.activeFields;
        this.archInfo = params.archInfo;
        this.defaultOrderBy = this.archInfo.defaultOrder || [];
        this.records = [];
        this.isTruncated = false;
        this.unscheduledCount = null;
        this.config = {
            domain: params.domain || [],
            context: params.context || {},
            orderBy: [],
            // The window the view is on, when the caller knows one: it is what
            // keeps a table of thousands of records from being read at once.
            window: params.window || null,
        };
    }

    get context() {
        return { ...this.config.context };
    }

    /**
     * Order applied to the rows: the one the user chose in the search bar when
     * there is one, the view's `default_order` otherwise.
     */
    get orderBy() {
        return this.config.orderBy.length ? this.config.orderBy : this.defaultOrderBy;
    }

    async load(params = {}) {
        const config = { ...this.config };
        if ("domain" in params) {
            config.domain = params.domain || [];
        }
        if ("context" in params) {
            config.context = params.context || {};
        }
        if ("orderBy" in params) {
            config.orderBy = params.orderBy || [];
        }
        this.config = config;
        await this._load(config);
    }

    /** Re-read the records with the parameters already in use. */
    async reload() {
        await this._load(this.config);
    }

    /**
     * Move the view to another window and re-read it.
     *
     * @param {{start: number, end: number}} window timestamps in milliseconds
     */
    async setWindow(window) {
        this.config.window = window;
        await this._load(this.config);
    }

    /**
     * Fetch records while only letting the newest request update the model.
     *
     * `KeepLast` deliberately leaves superseded promises pending. Returning that
     * promise from `load` therefore leaves callers such as a write-then-reload
     * flow waiting forever when a newer load starts. A request id gives every
     * caller a settling promise while still ignoring stale results.
     */
    async _load(config) {
        const loadId = ++this.loadId;
        const { records, isTruncated, unscheduledCount } = await this._fetch(config);
        if (loadId === this.loadId) {
            this.records = records;
            this.isTruncated = isTruncated;
            this.unscheduledCount = unscheduledCount;
            this.notify();
        }
    }

    /**
     * Domain keeping the records that overlap the window.
     *
     * The date shapes are kept, because a record may carry both dates, only
     * its start or only its stop:
     *
     * - both dates: the half-open intervals overlap, `dateStart < end` and
     *   `dateStop > start`;
     * - a zero-length point exactly at the window start is included;
     * - only a start: `dateStart` falls inside the window;
     * - only a stop: `dateStop` falls inside the window.
     *
     * The domain is built with the `Domain` combinators, never as a hand
     * written prefix list: the OR of these shapes is easy to get wrong by
     * hand and the server refuses a malformed list with a `ValueError` before
     * any record is read.
     */
    _windowDomain(window) {
        if (!window) {
            return [];
        }
        const { dateStart, dateStop, dateStartType, dateStopType } = this.archInfo;
        // Each comparison is serialised for the type of the field it filters:
        // a `date` boundary is a calendar day, a `datetime` boundary an instant.
        const startAtStart = serializeOdooDate(window.start, dateStartType);
        const endAtStart = serializeOdooDate(window.end, dateStartType);
        const startAtStop = serializeOdooDate(window.start, dateStopType);
        const endAtStop = serializeOdooDate(window.end, dateStopType);
        return Domain.or([
            [
                [dateStart, "<", endAtStart],
                [dateStop, ">", startAtStop],
            ],
            // A zero-length record exactly on the window's first instant is a
            // visible point, even though its half-open interval has no overlap.
            [
                [dateStart, "=", startAtStart],
                [dateStop, "=", startAtStop],
            ],
            [
                [dateStart, "=", false],
                [dateStop, ">=", startAtStop],
                [dateStop, "<", endAtStop],
            ],
            [
                [dateStop, "=", false],
                [dateStart, ">=", startAtStart],
                [dateStart, "<", endAtStart],
            ],
        ]).toList();
    }

    async _fetch(config) {
        const domain = Domain.and([
            config.domain,
            this._windowDomain(config.window),
        ]).toList();
        const limit = this.archInfo.limit;
        const [result, unscheduledCount] = await Promise.all([
            this.orm.webSearchRead(this.resModel, domain, {
                specification: getFieldsSpec(this.activeFields, this.fields, config.context || {}),
                context: config.context,
                order: orderByToString(this.orderBy),
                // Read one extra record so a truncated window is reported
                // instead of silently looking complete.
                limit: limit + 1,
            }),
            config.window
                ? this.orm.searchCount(
                      this.resModel,
                      Domain.and([
                          config.domain,
                          [
                              [this.archInfo.dateStart, "=", false],
                              [this.archInfo.dateStop, "=", false],
                          ],
                      ]).toList(),
                      { context: config.context }
                  )
                : Promise.resolve(null),
        ]);
        const isTruncated = result.records.length > limit;
        return {
            records: isTruncated ? result.records.slice(0, limit) : result.records,
            isTruncated,
            // Without a window every record is loaded, so the renderer can
            // count undated records itself. With a window they are outside its
            // domain and need the separate count above.
            unscheduledCount,
        };
    }
}
