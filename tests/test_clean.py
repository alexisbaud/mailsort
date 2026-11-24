from pathlib import Path

import pytest

from mailsort.clean import (
    MAX_CHARS,
    TRUNCATION_MARK,
    attachment_names,
    bodies_from_message,
    choose_version,
    clean_email_text,
    html_to_text,
    load_eml,
    remove_invisible,
    strip_formatting,
    strip_reply_history,
    strip_signature,
    truncate,
)

FIXTURES = Path(__file__).parent / "fixtures"


def clean_fixture(name: str, *, only_html: bool = False):
    message = load_eml(FIXTURES / name)
    plain, html = bodies_from_message(message)
    if only_html:
        plain = None
    return clean_email_text(plain, html, subject=str(message["subject"]))


# ============================================================ fixtures .eml


class TestInvisiblePreheader:
    def test_raw_html_is_mostly_invisible_padding(self):
        # Garde-fou du test : tronquer avant de nettoyer ne laisserait que du remplissage.
        _, html = bodies_from_message(load_eml(FIXTURES / "preheader_invisible.eml"))
        assert len(html) > MAX_CHARS
        assert "Bienvenue" not in html[:MAX_CHARS]

    def test_content_comes_first(self):
        result = clean_fixture("preheader_invisible.eml")
        assert result.source == "html"
        assert result.text.startswith("Bienvenue dans notre boutique")
        assert "Votre code de bienvenue est BIENVENUE10, valable jusqu'au 30 novembre." in (
            result.text
        )
        assert "Découvrir la collection" in result.text
        assert not result.truncated

    @pytest.mark.parametrize(
        "unwanted",
        ["Aperçu caché", "Titre ignoré", "alert", "commentaire", "http", "Logo", "color"],
    )
    def test_hidden_and_technical_content_removed(self, unwanted):
        assert unwanted not in clean_fixture("preheader_invisible.eml").text

    def test_no_invisible_characters_left(self):
        text = clean_fixture("preheader_invisible.eml").text
        assert not any(ch in text for ch in "​‌͏­ ")
        assert len(text) < 200


class TestReplyWithHistory:
    @pytest.mark.parametrize("only_html", [False, True], ids=["plain", "html"])
    def test_only_new_message_is_kept(self, only_html):
        result = clean_fixture("reply_with_history.eml", only_html=only_html)
        assert result.source == ("html" if only_html else "plain")
        assert result.text == (
            "Parfait, je réserve pour 20h. Tu peux confirmer pour Paul ?\n\nBises,\nMarie"
        )
        assert not result.forwarded


class TestForward:
    def test_forwarded_content_is_kept(self):
        result = clean_fixture("forward.eml")
        assert result.forwarded
        text = result.text
        assert text.startswith("Salut, je te transfère l'attestation")
        assert "De : Agence Exemple Immobilier" in text
        assert "Subject: Votre attestation de loyer" in text
        assert "Veuillez trouver ci-joint l'attestation de loyer" in text
        assert text.endswith("Cordialement,\nL'agence")

    def test_history_of_forwarded_message_is_removed(self):
        text = clean_fixture("forward.eml").text
        assert "pourriez-vous" not in text
        assert "a écrit" not in text
        assert "Forwarded message" not in text

    def test_attachment_is_listed_not_read(self):
        message = load_eml(FIXTURES / "forward.eml")
        assert attachment_names(message) == ["attestation_loyer.pdf"]
        plain, html = bodies_from_message(message)
        assert "PDF" not in plain
        assert html is None


class TestLongNewsletter:
    @pytest.mark.parametrize("only_html", [False, True], ids=["plain", "html"])
    def test_truncated_to_limit_keeping_the_start(self, only_html):
        result = clean_fixture("long_newsletter.eml", only_html=only_html)
        assert result.truncated
        assert len(result.text) <= MAX_CHARS
        assert result.text.endswith(TRUNCATION_MARK)
        assert result.text.lower().startswith("la lettre du dimanche")
        assert "Titre de l'article 1" in result.text
        assert "Se désinscrire" not in result.text  # fin du mail coupée

    @pytest.mark.parametrize("only_html", [False, True], ids=["plain", "html"])
    def test_links_and_table_bars_removed(self, only_html):
        text = clean_fixture("long_newsletter.eml", only_html=only_html).text
        assert "http" not in text
        assert "|" not in text
        assert "=====" not in text
        assert "-----" not in text
        assert "Lire la suite" in text
        assert "Indicateur Valeur\nLecteurs 12 000" in text

    def test_plain_version_preferred_when_it_has_content(self):
        assert clean_fixture("long_newsletter.eml").source == "plain"


# ================================================================== étape 1


def test_plain_placeholder_falls_back_to_html():
    html = "<p>" + "Contenu riche de la lettre. " * 30 + "</p>"
    source, text = choose_version("Ouvrez ce mail dans votre navigateur.", html)
    assert source == "html"
    assert text.startswith("Contenu riche")


def test_short_plain_kept_when_html_is_similar():
    source, _ = choose_version("Ok pour demain.", "<div>Ok pour demain.</div>")
    assert source == "plain"


def test_plain_with_only_invisible_characters_falls_back_to_html():
    source, _ = choose_version("​‌ \n", "<p>Bonjour</p>")
    assert source == "html"


def test_empty_mail():
    result = clean_email_text(None, None)
    assert result.text == ""
    assert result.source == "empty"


@pytest.mark.parametrize(
    "style",
    [
        "display:none",
        "display: none !important",
        "visibility:hidden",
        "max-height:0;overflow:hidden",
        "max-height: 0px",
        "opacity:0",
    ],
)
def test_hidden_elements_removed(style):
    html = f'<div style="{style}">caché</div><p>visible</p>'
    assert html_to_text(html) == "visible"


def test_font_size_zero_container_is_kept():
    # Technique de mise en page courante : le texte est remis à taille normale dans l'enfant.
    html = '<td style="font-size:0"><div style="font-size:14px">visible</div></td>'
    assert html_to_text(html) == "visible"


def test_html_structure():
    html = (
        "<div>Ligne 1</div><div>Ligne 2</div><div><br></div><div>Après un saut</div>"
        "<p>Paragraphe</p><ul><li>a</li><li>b</li></ul>"
        "<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>"
    )
    assert html_to_text(html) == (
        "Ligne 1\nLigne 2\n\nAprès un saut\n\nParagraphe\n\na\nb\n\nA B\nC D"
    )


def test_html_blockquote_is_prefixed():
    assert html_to_text("<p>Réponse</p><blockquote><p>Citation</p></blockquote>") == (
        "Réponse\n\n> Citation"
    )


# ================================================================== étape 2


def test_remove_invisible_keeps_accents_and_emoji():
    text = "Café​ été ­🎉﻿͏⁠"
    assert remove_invisible(text) == "Café été 🎉"


def test_remove_invisible_normalizes_spaces_and_newlines():
    assert remove_invisible("a b c\r\nd\re\tf") == "a b c\nd\ne\tf"


# ================================================================== étape 3


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Voir https://example.com/x?a=1 ici", "Voir ici"),
        ("Site : www.example.com", "Site :"),
        ("[Lire la suite](https://example.com/a)", "Lire la suite"),
        ("Lire la suite <https://example.com/a>", "Lire la suite"),
        ("Lire [https://example.com/a]", "Lire"),
        ("Écrire à <mailto:contact@example.com>", "Écrire à"),
        ("Lien (https://example.com)", "Lien"),
        ("| a | b |", "a b"),
        ("+---+---+\n|---|---|\nok", "ok"),
        ("──────────\nok\n**********", "ok"),
        ("a\n\n\n\nb", "a\n\nb"),
        ("  a    b  ", "a b"),
    ],
)
def test_strip_formatting(raw, expected):
    assert strip_formatting(raw) == expected


def test_strip_formatting_keeps_signature_delimiter_and_markers():
    text = "a\n--\nb\n---------- Forwarded message ---------\nc"
    assert strip_formatting(text) == text


# ============================================================== étapes 4-6


def history(text, subject=""):
    return strip_reply_history(text, subject=subject)


@pytest.mark.parametrize(
    "attribution",
    [
        "Le lun. 23 nov. 2026 à 18:02, Alexis <a@example.com> a écrit :",
        "Le 23/11/2026 à 18:02, Alexis a écrit :",
        "On Mon, Nov 23, 2026 at 6:02 PM Alexis <a@example.com> wrote:",
        "On Mon, Nov 23, 2026 at 6:02 PM Alexis <\na@example.com> wrote:",
        "-----Original Message-----",
        "----- Message d'origine -----",
        "De : Alexis <a@example.com>\nEnvoyé : lundi 23 novembre 2026 18:02\n"
        "À : Marie <m@example.org>\nObjet : Dîner",
        "From: Alexis <a@example.com>\nSent: Monday, November 23, 2026 6:02 PM\n"
        "To: Marie <m@example.org>\nSubject: Dinner",
        "De : Alexis <a@example.com>\nDate : lundi 23 novembre 2026 à 18:02\n"
        "À : Marie <m@example.org>\nObjet : Dîner",
    ],
    ids=[
        "gmail-fr",
        "apple-fr",
        "gmail-en",
        "gmail-en-wrapped",
        "original-en",
        "original-fr",
        "outlook-fr",
        "outlook-en",
        "outlook-mac-fr",
    ],
)
def test_reply_history_removed(attribution):
    text = f"Nouveau message.\n\n{attribution}\nAncien message.\n> cité"
    assert history(text) == ("Nouveau message.", False)


def test_quoted_lines_removed_in_reply():
    assert history("Oui.\n> Tu viens ?", subject="Re: Samedi") == ("Oui.", False)


def test_quotes_kept_when_not_a_reply():
    text = "Citation du jour :\n> Le savoir est une arme."
    assert history(text) == ("Citation du jour :\nLe savoir est une arme.", False)


def test_sentence_starting_with_le_is_not_an_attribution():
    text = "Le rendez-vous est confirmé.\nLe médecin vous attend à 9h."
    assert history(text) == (text, False)


def test_header_lines_without_subject_are_not_history():
    # Notification de formulaire de contact : De / Date mais pas d'objet.
    text = "Nouveau message :\nDe : Jean\nDate : 23/11/2026\nMessage : Bonjour"
    assert history(text)[0] == text


def test_apple_forward_with_quoted_body():
    text = (
        "Pour info.\n\nDébut du message réexpédié :\n\n"
        "> De : Banque <info@banque.example.com>\n> Objet : Relevé\n> Date : 1 nov. 2026\n"
        "> À : Jean <j@example.net>\n>\n> Votre relevé est disponible."
    )
    result, forwarded = history(text, subject="Fwd: Relevé")
    assert forwarded
    assert result == (
        "Pour info.\n\nDe : Banque <info@banque.example.com>\nObjet : Relevé\n"
        "Date : 1 nov. 2026\nÀ : Jean <j@example.net>\n\nVotre relevé est disponible."
    )


def test_outlook_forward_detected_by_subject():
    text = (
        "Voir ci-dessous.\n\nDe : RH <rh@example.com>\nEnvoyé : lundi 2 novembre 2026\n"
        "À : Marie <m@example.org>\nObjet : Bulletin de paie\n\nVotre bulletin est disponible."
    )
    result, forwarded = history(text, subject="TR: Bulletin de paie")
    assert forwarded
    assert "Votre bulletin est disponible." in result


def test_reply_to_a_forward_drops_the_forward():
    text = (
        "Merci !\n\nLe lun. 23 nov. 2026 à 18:02, Jean <j@example.net> a écrit :\n"
        "> Pour info.\n> ---------- Forwarded message ---------\n> De : X\n> blabla"
    )
    assert history(text, subject="Re: Fwd: Info") == ("Merci !", False)


def test_forward_note_signature_does_not_cut_forwarded_content():
    text = (
        "Regarde ça.\n--\nJean\n\n---------- Message transféré ---------\n"
        "De : Club <club@example.org>\nDate : 1 nov.\nObjet : Tournoi\nÀ : Jean\n\n"
        "Inscriptions ouvertes."
    )
    result, forwarded = history(text)
    assert forwarded
    assert result.startswith("Regarde ça.\n\nDe : Club")
    assert result.endswith("Inscriptions ouvertes.")


@pytest.mark.parametrize(
    "line",
    [
        "Envoyé de mon iPhone",
        "Sent from my iPhone",
        "Envoyé depuis mon appareil Galaxy",
        "Get Outlook for iOS",
        "Télécharger Outlook pour Android",
        "Sent from Mail for Windows",
    ],
)
def test_mobile_signatures_removed(line):
    assert strip_signature(f"Ok pour moi.\n\n{line}") == "Ok pour moi."


def test_signature_after_delimiter_removed():
    assert strip_signature("Bonjour.\n--\nMarie\n06 00 00 00 00") == "Bonjour."


def test_long_text_after_delimiter_is_kept():
    text = "Intro\n--\n" + "\n".join(f"Ligne {i}" for i in range(20))
    assert strip_signature(text) == text


# ================================================================== étape 7


def test_truncate_keeps_short_text():
    assert truncate("court", 100) == ("court", False)


def test_truncate_cuts_on_whitespace_with_mark():
    text = "mot " * 100
    result, truncated = truncate(text, 50)
    assert truncated
    assert len(result) <= 50
    assert result.endswith(TRUNCATION_MARK)
    assert result.removesuffix(TRUNCATION_MARK).split(" ")[-1] == "mot"


def test_pipeline_cleans_before_truncating():
    html = "<span>" + "‌ " * 30_000 + "</span><p>Contenu utile</p>"
    result = clean_email_text(None, html)
    assert result.text == "Contenu utile"
    assert not result.truncated
