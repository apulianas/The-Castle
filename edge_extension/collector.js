/* This function is serialized into the approved tab's isolated world. */
async function extractRavensPage(scroll) {
    const approved = () => {
        const url = new URL(location.href);
        return url.protocol === "https:" && url.hostname === "x.com" &&
            !url.port && !url.username && !url.password && !url.hash &&
            /^\/ravens\/?$/i.test(url.pathname);
    };
    const check = () => {
        if (!approved()) throw new Error("page_changed");
        if (!document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]')) {
            throw new Error("sign_in_required");
        }
        if (document.querySelector(
            '[data-testid="challenge"], iframe[src*="captcha"], iframe[src*="arkoselabs"]'
        )) throw new Error("challenge");
    };
    // Allow the initial signed-in timeline to render, but never interact with a login/challenge.
    if (!approved()) return {error: "page_changed"};
    try {
        if (scroll) {
            check();
            window.scrollBy(0, 1400);
        } else window.scrollTo(0, 0);
        // A timer chain can be throttled once per second/minute in background tabs.
        // One settling timer avoids multiplying that delay; check the URL again on wake.
        await new Promise(resolve => setTimeout(resolve, 1000));
        check();
        const posts = [];
        let truncated = 0;
        let oversized = 0;
        const diagnostics = {
            articles_seen: 0, nested_articles: 0, time_nodes: 0, own_timestamp_links: 0,
            quote_time_rejections: 0, missing_outer_time: 0, other_author_or_link: 0,
            social_rejections: 0, quote_text_skipped: 0, quote_images_skipped: 0,
            truncated: 0, oversized: 0, bad_date: 0, empty_content: 0, accepted: 0
        };
        const ownLink = link => link &&
            /^https:\/\/x\.com\/ravens\/status\/\d+$/i.test(link.href);
        for (const article of document.querySelectorAll('article[data-testid="tweet"]')) {
            diagnostics.articles_seen++;
            if (article.parentElement.closest('article[data-testid="tweet"]')) {
                diagnostics.nested_articles++;
                continue;
            }
            const quoted = node => {
                for (let p = node.parentElement; p && p !== article; p = p.parentElement) {
                    // X also gives the timestamp's own anchor role="link"; that is not a quote card.
                    const timestampAnchor = node.matches("time") && p === node.closest("a") &&
                        p.querySelector("time") === node && p.textContent.trim() === node.textContent.trim() &&
                        !p.querySelector('img, [data-testid="tweetText"], [data-testid="tweetPhoto"], [data-testid="quoteTweet"]');
                    if (p.matches('article, [data-testid="quoteTweet"]') ||
                        (!timestampAnchor && p.getAttribute("role") === "link" &&
                            p.querySelector("time"))) return true;
                }
                return false;
            };
            const times = Array.from(article.querySelectorAll("time"));
            diagnostics.time_nodes += times.length;
            diagnostics.own_timestamp_links += times.filter(node => ownLink(node.closest("a"))).length;
            const outerTimes = times.filter(node => {
                if (!quoted(node)) return true;
                diagnostics.quote_time_rejections++;
                return false;
            });
            const time = outerTimes[0];
            if (!time) {
                diagnostics.missing_outer_time++;
                continue;
            }
            const link = time?.closest("a");
            if (!ownLink(link)) {
                diagnostics.other_author_or_link++;
                continue;
            }
            const social = Array.from(article.querySelectorAll('[data-testid="socialContext"]'))
                .filter(node => !quoted(node));
            // A pin icon is distinct from the repost icon; unknown social context is not trusted.
            if (social.some(node => node.querySelector('[data-testid="icon-retweet"]') ||
                (!node.querySelector('[data-testid="icon-pin"]') &&
                    !/^pinned$/i.test(node.innerText.trim())))) {
                diagnostics.social_rejections++;
                continue;
            }
            const outer = selector => Array.from(article.querySelectorAll(selector))
                .filter(node => !quoted(node));
            diagnostics.quote_text_skipped += Array.from(
                article.querySelectorAll('[data-testid="tweetText"]')).filter(quoted).length;
            diagnostics.quote_images_skipped += Array.from(
                article.querySelectorAll('[data-testid="tweetPhoto"] img')).filter(quoted).length;
            if (outer('[data-testid="tweet-text-show-more-link"]').length ||
                outer('button, a, [role="button"]').some(node =>
                    /^(show more|read more)$/i.test(node.innerText.trim()))) {
                truncated++;
                diagnostics.truncated++;
                continue;
            }
            const text = outer('[data-testid="tweetText"]').map(node => node.innerText).join("\n");
            if (text.length > 25000) {
                oversized++;
                diagnostics.oversized++;
                continue;
            }
            const createdAt = time.getAttribute("datetime") || "";
            if (!/^\d{4}-\d\d-\d\dT.*(?:Z|[+-]\d\d:\d\d)$/.test(createdAt) ||
                !Number.isFinite(Date.parse(createdAt))) {
                diagnostics.bad_date++;
                continue;
            }
            const imageUrls = [...new Set(outer('[data-testid="tweetPhoto"] img')
                .map(node => node.src).filter(value => {
                    try {
                        const url = new URL(value);
                        return url.protocol === "https:" && url.hostname === "pbs.twimg.com" &&
                            !url.port && !url.username && !url.password &&
                            url.pathname.startsWith("/media/");
                    } catch { return false; }
                }))].slice(0, 4);
            if (!text.trim() && !imageUrls.length) {
                diagnostics.empty_content++;
                continue;
            }
            posts.push({
                url: `https://x.com/Ravens/status/${link.href.split("/").pop()}`,
                text, created_at: createdAt, image_urls: imageUrls
            });
            diagnostics.accepted++;
        }
        check();
        for (const key of Object.keys(diagnostics)) {
            diagnostics[key] = Math.min(diagnostics[key], 99999);
        }
        return {posts, truncated, oversized, diagnostics};
    } catch (error) {
        return {error: ["page_changed", "sign_in_required", "challenge"].includes(error.message)
            ? error.message : "extraction_failed"};
    }
}
