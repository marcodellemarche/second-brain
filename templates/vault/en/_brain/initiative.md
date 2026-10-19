---
type: brain
created: YYYY-MM-DD
updated: YYYY-MM-DD
---

# Initiative

How far the brain may come forward on its own, per category. `budget` is the maximum number of proactive notifications per day for that category, and the categories together never exceed `total`. When I say "you are too noisy", the number goes down: that is how the level is tuned over time.

The brain never raises these numbers on its own, and the morning and evening messages do not count against the cap.

```yaml
total: 6
categories:
  deadlines: 2
  digest: 2
  ideas: 1
  reminders: 1
```
