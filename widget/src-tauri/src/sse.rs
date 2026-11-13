//! Incremental Server-Sent Events parser (text/event-stream), fed with raw byte chunks.
//! Handles chunks split mid-line or mid-UTF-8 sequence, CRLF, comments and multi-line data.
//! Bounded (M5): a line longer than MAX_LINE or an event larger than MAX_EVENT is dropped
//! (with the event it belongs to), so a misbehaving peer cannot grow memory without limit.

pub const MAX_LINE: usize = 64 * 1024;
pub const MAX_EVENT: usize = 1024 * 1024;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SseEvent {
    pub event: String,
    pub data: String,
}

#[derive(Default)]
pub struct SseParser {
    pending: Vec<u8>,
    event: String,
    data: Vec<String>,
    data_len: usize,
    /// The current line is too long: drop bytes up to its newline.
    skip_line: bool,
    /// The current event lost a line or grew too big: drop it at its end.
    poisoned: bool,
}

const DEFAULT_EVENT: &str = "message";

impl SseParser {
    /// Feed a chunk; returns every event completed by it.
    pub fn feed(&mut self, chunk: &[u8]) -> Vec<SseEvent> {
        self.pending.extend_from_slice(chunk);
        let mut out = Vec::new();
        while let Some(pos) = self.pending.iter().position(|&b| b == b'\n') {
            let line: Vec<u8> = self.pending.drain(..=pos).collect();
            if self.skip_line {
                self.skip_line = false;
                continue;
            }
            if line.len() - 1 > MAX_LINE {
                self.poisoned = true;
                continue;
            }
            let text = String::from_utf8_lossy(&line[..line.len() - 1]);
            let text = text.strip_suffix('\r').unwrap_or(&text).to_string();
            if let Some(event) = self.line(&text) {
                out.push(event);
            }
        }
        if self.pending.len() > MAX_LINE {
            self.pending.clear();
            self.skip_line = true;
            self.poisoned = true;
        }
        out
    }

    fn line(&mut self, line: &str) -> Option<SseEvent> {
        if line.is_empty() {
            return self.dispatch();
        }
        if line.starts_with(':') {
            return None; // comment / heartbeat
        }
        let (field, value) = match line.split_once(':') {
            Some((field, value)) => (field, value.strip_prefix(' ').unwrap_or(value)),
            None => (line, ""),
        };
        match field {
            "event" => self.event = value.to_string(),
            "data" => {
                self.data_len += value.len() + 1;
                if self.data_len > MAX_EVENT {
                    self.poisoned = true;
                    self.data.clear();
                } else if !self.poisoned {
                    self.data.push(value.to_string());
                }
            }
            _ => {} // id / retry / unknown fields are not used by the widget
        }
        None
    }

    fn dispatch(&mut self) -> Option<SseEvent> {
        let event = std::mem::take(&mut self.event);
        let data = std::mem::take(&mut self.data);
        let poisoned = self.poisoned;
        self.poisoned = false;
        self.data_len = 0;
        if data.is_empty() || poisoned {
            return None;
        }
        Some(SseEvent {
            event: if event.is_empty() { DEFAULT_EVENT.into() } else { event },
            data: data.join("\n"),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_named_event() {
        let mut p = SseParser::default();
        let out = p.feed(b"event: session\ndata: {\"id\":\"a\"}\n\n");
        assert_eq!(out, vec![SseEvent { event: "session".into(), data: "{\"id\":\"a\"}".into() }]);
    }

    #[test]
    fn handles_split_chunks_crlf_and_comments() {
        let mut p = SseParser::default();
        assert!(p.feed(b": heartbeat\r\nevent: st").is_empty());
        assert!(p.feed(b"ep\r\ndata: x\r\n").is_empty());
        let out = p.feed(b"\r\n");
        assert_eq!(out[0].event, "step");
        assert_eq!(out[0].data, "x");
    }

    #[test]
    fn joins_multiline_data_and_defaults_event_name() {
        let mut p = SseParser::default();
        let out = p.feed(b"data: a\ndata: b\n\n");
        assert_eq!(out[0].event, "message");
        assert_eq!(out[0].data, "a\nb");
    }

    #[test]
    fn keeps_utf8_split_across_chunks() {
        let mut p = SseParser::default();
        let bytes = "data: é\n\n".as_bytes();
        let (a, b) = bytes.split_at(7); // splits the two bytes of "é"
        assert!(p.feed(a).is_empty());
        assert_eq!(p.feed(b)[0].data, "é");
    }

    #[test]
    fn empty_block_dispatches_nothing() {
        let mut p = SseParser::default();
        assert!(p.feed(b"event: x\n\n").is_empty());
        assert_eq!(p.feed(b"data: y\n\n")[0].event, "message");
    }

    #[test]
    fn overlong_line_drops_its_event_and_memory() {
        let mut p = SseParser::default();
        let mut huge = b"event: big\ndata: ".to_vec();
        huge.extend(std::iter::repeat(b'x').take(MAX_LINE + 10));
        assert!(p.feed(&huge).is_empty());
        assert!(p.pending.len() <= MAX_LINE);
        assert!(p.feed(b"xxxx\n\n").is_empty()); // end of the dropped line and event
        assert_eq!(p.feed(b"event: ok\ndata: 1\n\n")[0].event, "ok");
    }

    #[test]
    fn overlong_line_in_one_chunk_is_dropped_too() {
        let mut p = SseParser::default();
        let mut chunk = b"data: ".to_vec();
        chunk.extend(std::iter::repeat(b'y').take(MAX_LINE + 1));
        chunk.extend(b"\n\nevent: ok\ndata: 2\n\n");
        let out = p.feed(&chunk);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].data, "2");
    }

    #[test]
    fn oversized_event_is_dropped() {
        let mut p = SseParser::default();
        let line = format!("data: {}\n", "z".repeat(MAX_LINE - 100));
        let mut out = Vec::new();
        for _ in 0..(MAX_EVENT / MAX_LINE + 2) {
            out.extend(p.feed(line.as_bytes()));
        }
        out.extend(p.feed(b"\n"));
        assert!(out.is_empty());
        assert_eq!(p.feed(b"data: small\n\n")[0].data, "small");
    }
}
