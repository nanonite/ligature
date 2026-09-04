// Fixture crate for the C_static extractor (chainlink #24).
//
// Every construct here exists to pin one classification decision, and
// several exist to pin something a regex-only extractor gets wrong:
// a call inside a string, a call inside a comment, a lifetime that a
// naive char-literal scanner would use to swallow real code.
//
// This file is never compiled by CI -- it is input to a source-level
// extractor, and pinning the extractor's behaviour is the point.
use std::collections::HashMap;

pub enum Slot {
    Ready(u64),
    Empty,
}

pub struct Scheduler {
    queue: TaskQueue,
    hook: fn(u64) -> bool,
}

impl Scheduler {
    pub fn dispatch(&mut self, now: u64) -> bool {
        // not a call: never_called(
        let label = "also not a call: never_called(";
        let task = TaskQueue::pop_ready(&mut self.queue, now);
        if self.validate(now) {
            self.queue.push_back(task);
        }
        let slot = Slot::Ready(task);
        let hook = self.hook;
        hook(now);
        println!("dispatched {} {:?}", label, matches!(slot, Slot::Empty));
        run(now)
    }

    fn validate(&self, now: u64) -> bool {
        now > 0
    }
}

impl<'a> Clone for Scheduler {
    fn clone(&'a self) -> Self {
        Scheduler {
            queue: TaskQueue::new(),
            hook: self.hook,
        }
    }
}

pub struct TaskQueue {
    items: Vec<u64>,
}

impl TaskQueue {
    pub fn new() -> Self {
        TaskQueue { items: Vec::new() }
    }

    pub fn pop_ready(&mut self, now: u64) -> u64 {
        self.items.pop().unwrap_or(now)
    }

    pub fn push_back(&mut self, value: u64) {
        self.items.push(value);
    }
}

fn run(now: u64) -> bool {
    let seen: HashMap<u64, u64> = HashMap::new();
    seen.len() as u64 > now
}
