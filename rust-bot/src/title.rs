// Treat user input as one literal title phrase, not Mongo's quote/minus syntax.
pub fn title_phrase(raw: &str) -> Option<String> {
    let input: String = raw
        .chars()
        .take(120)
        .map(|c| {
            if c.is_alphanumeric() || is_mark(c) {
                c
            } else {
                ' '
            }
        })
        .collect();
    let phrase = input.split_whitespace().collect::<Vec<_>>().join(" ");
    phrase
        .chars()
        .any(char::is_alphanumeric)
        .then(|| format!("\"{phrase}\""))
}

fn is_mark(c: char) -> bool {
    // Retain Malayalam combining marks, which aren't considered alphanumeric.
    matches!(c, '\u{0d00}'..='\u{0d03}' | '\u{0d3b}'..='\u{0d4d}' | '\u{0d57}' | '\u{0d62}'..='\u{0d63}')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_legacy_separators_and_ignores_text_operators() {
        assert_eq!(
            title_phrase("  Spider-Man.2021_1080p+WEB \"-DL\"").as_deref(),
            Some("\"Spider Man 2021 1080p WEB DL\"")
        );
        assert_eq!(title_phrase("-._+\""), None);
    }

    #[test]
    fn preserves_malayalam_and_caps_input() {
        assert_eq!(title_phrase("സിനിമ").as_deref(), Some("\"സിനിമ\""));
        assert_eq!(title_phrase(&"x".repeat(140)).unwrap().len(), 122);
    }
}
