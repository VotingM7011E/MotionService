# MotionService

```
voting_session: {
  state: "in_progress" | "completed" | "error",
  motion_queue: [motion_uuid1, motion_uuid2, ...],
  current_index: 0,
  poll_history: [
    {motion_uuid, poll_uuid, results, total_votes, completed_at},
    ...
  ],
  started_at: timestamp,
  completed_at: timestamp
}

poll: {  // Current active poll
  poll_uuid: "...",
  poll_state: "open" | "completed",
  motion_uuid: "...",
  created_at: timestamp
}
```
