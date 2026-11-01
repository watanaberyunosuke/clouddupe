//! Duplicate detection over cloud drive listings.
//!
//! Input is the JSON emitted by `rclone lsjson -R --files-only --hash`.
//! Files are compared by the content hashes the provider already stores, so
//! nothing is downloaded.
//!
//! Confidence levels:
//! - `Confirmed`: same size and an identical server-side hash
//! - `Probable`: same size and normalised name, but content cannot be proven
//!   equal because no hash type is shared (or a hash is missing)
//! - `Possible`: same size only, no shared hash (opt-in)

mod detect;
mod model;
mod normalise;
mod report;
mod summary;

pub use detect::{detect, DetectOptions};
pub use model::{parse_item, Confidence, FileEntry, Group};
pub use normalise::{normalise_hash_type, normalise_name};
pub use report::{to_csv, to_html, to_json};
pub use summary::{summarise, ComparablePair, ConfidenceStats, RemoteStats, Summary};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
