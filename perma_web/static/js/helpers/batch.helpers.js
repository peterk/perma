var BatchViewModule = require('../batch-view.module.js');

export function human_timestamp_from_batch(batch) {
    return new Date(batch.started_on).toLocaleString("en-us", {
        year:   "numeric",
        month:  "long",
        day:    "numeric",
        hour:   "numeric",
        minute: "2-digit"
    });
}

export function show_modal_with_batch(batch) {
    BatchViewModule.show_batch(batch.id, batch.saved_folder);
    $("#batch-view-modal").modal("show");
}
