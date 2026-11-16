// Fixture crate for the C_static extractor's ARRAY RETURN TYPES (chainlink
// #170).
//
// `[u8; N]` is a ubiquitous crypto return type -- tags, keys, blocks. The
// structural pass used to mistake the `;` inside it for the end of a
// bodyless signature, so every array-returning method vanished: callers
// inside it were never scanned and calls to it were counted out of scope.
// That hid the real cross-concept AEAD -> Poly1305 edge in the crypto-mixed
// pilot.
//
// Like the sibling crate_scheduler fixture this file is never compiled by
// CI -- it is input to a source-level extractor, and pinning the
// extractor's behaviour is the point.

pub struct Aead {
    key: [u8; 32],
}

impl Aead {
    // self-method-call to an array-returning method of the same concept.
    pub fn verify(&self, supplied: &[u8; 16]) -> bool {
        let expected = self.tag();
        expected == *supplied
    }

    // The array-returning caller whose own body holds the cross-concept
    // edge. `tag` is a scanned caller only if its `[u8; 16]` does not
    // terminate its signature.
    pub fn tag(&self) -> [u8; 16] {
        Poly::compute_tag_segments(&self.key, &[])
    }

    // An array PARAMETER is the same trap on the way in.
    pub fn rotate(key: [u8; 32]) -> [u8; 32] {
        Poly::expand(key)
    }
}

pub struct Poly;

impl Poly {
    // associated-path-call callee returning an array.
    pub fn compute_tag_segments(key: &[u8; 32], segments: &[&[u8]]) -> [u8; 16] {
        let _ = segments;
        *key
    }

    pub fn expand(key: [u8; 32]) -> [u8; 32] {
        key
    }
}

// An impl-level method this extractor cannot index: there is no body to
// locate, so `missing` is declared but absent from the method index. It
// stands in for any future index gap (a syntax the structural pass does
// not understand). A call to it must surface as unresolved, never be
// silently counted out of scope -- the #170 soundness rule. (Only the
// bodyless `missing` is deliberately not compilable; this fixture is input
// to a tokenizer, not to rustc.)
pub struct Unindexed;

impl Unindexed {
    pub fn missing(&self) -> [u8; 16];

    pub fn caller(&self) -> bool {
        self.missing() == self.missing()
    }
}
