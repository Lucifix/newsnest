from fastapi.testclient import TestClient

from newsnest import REPO_URL, __version__


def test_about_page_links_to_the_repo_and_prefills_the_version(env, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    import newsnest.web as web
    with TestClient(web.app) as client:
        page = client.get("/settings/about").text
        assert f"v{__version__}" in page and REPO_URL in page
        assert f"{REPO_URL}/issues/new?template=bug_report.yml&amp;version={__version__}" in page
        # the version line sits under the menu on every settings page, About also works while the config is broken
        assert "snav-version" in client.get("/settings/feeds").text
        (env.config / "config.yaml").write_text("feeds: [", encoding="utf-8")
        assert client.get("/settings/about").status_code == 200
