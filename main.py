import json
import time
import random
import requests
from datetime import datetime, timezone
import swarmnode

# ========================
# SWARMNODE & AI PAYLOAD
# ========================
swarmnode.api_key = "API_KEY"
twitterAgent = swarmnode.Agent.retrieve(id="AGENT_ID")

def execute_payload(payload):
    """
    Executes an AI payload (blocking). 
    Because this is single-threaded, if it takes up to a minute, 
    the entire script is paused during that time.
    """
    execution = twitterAgent.execute(wait=True, payload=payload)
    return execution.return_value["content"]

# ===============
# TOKENS IN A FILE
# ===============
TOKENS_FILE = "twitter_tokens.json"
def load_tokens():
    """
    Loads token data from `twitter_tokens.json`.
    Returns (access_token, refresh_token, client_id, client_secret).
    """
    with open(TOKENS_FILE, "r") as f:
        data = json.load(f)
    return (
        data["access_token"],
        data["refresh_token"],
        data["client_id"],
        data["client_secret"]
    )

def save_tokens(access_token, refresh_token, client_id, client_secret):
    """
    Overwrites `twitter_tokens.json` with new token values.
    """
    data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret
    }
    with open(TOKENS_FILE, "w") as f:
        json.dump(data, f)

# We'll store these globally after loading
ACCESS_TOKEN = None
REFRESH_TOKEN = None
CLIENT_ID = None
CLIENT_SECRET = None

# We'll store the user_id after we fetch it once
USER_ID = None

# ==========================
# HTTP UTIL & REFRESH LOGIC
# ==========================
def refresh_user_access_token():
    """
    Blocking function to refresh the user access token.
    Updates the global tokens and saves them back to the file.
    """
    global ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_ID, CLIENT_SECRET

    print("Attempting to refresh user access token...")

    url = "https://api.twitter.com/2/oauth2/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    resp = requests.post(url, data=data, headers=headers)
    if resp.status_code != 200:
        raise Exception(f"Failed to refresh access token: {resp.text}")

    token_response = resp.json()
    if "access_token" not in token_response:
        raise Exception("No access_token in token response.")

    # Update globals
    ACCESS_TOKEN = token_response["access_token"]
    if "refresh_token" in token_response:
        REFRESH_TOKEN = token_response["refresh_token"]

    # Save new tokens back to file
    save_tokens(ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_ID, CLIENT_SECRET)

    print("User access token refreshed successfully!")

def call_twitter_api(method, endpoint, params=None, json_body=None, retry=True):
    """
    Calls the Twitter API with the current ACCESS_TOKEN (blocking).
    - If a 401 occurs, we refresh once, then retry.
    """
    global ACCESS_TOKEN

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    if method.upper() == "GET":
        r = requests.get(endpoint, headers=headers, params=params)
    elif method.upper() == "POST":
        r = requests.post(endpoint, headers=headers, params=params, json=json_body)
    else:
        raise ValueError(f"Unsupported method: {method}")

    if r.status_code == 401 and retry:
        print("Got 401 Unauthorized. Refreshing access token...")
        refresh_user_access_token()
        return call_twitter_api(method, endpoint, params, json_body, retry=False)

    r.raise_for_status()
    return r.json()

def get_my_user_id():
    """
    Retrieves the user's ID from /2/users/me (v2),
    using our OAuth2 user token.
    """
    url = "https://api.twitter.com/2/users/me"
    data = call_twitter_api("GET", url)
    return data["data"]["id"]

# =============================
# POLLING THE v2 HOME TIMELINE
# =============================

script_start_time = datetime.now(timezone.utc)
processed_tweet_ids = set()  # track tweets we've processed
last_seen_id = None

def fetch_home_timeline(since_id=None, max_results=10):
    """
    Calls the v2 reverse chronological timeline:
    GET /2/users/{USER_ID}/timelines/reverse_chronological
    with expansions=author_id so we get each tweet's author info.
    Requires Early Access for the user.
    """
    if not USER_ID:
        raise Exception("USER_ID not set. Did you call get_my_user_id()?")

    url = f"https://api.twitter.com/2/users/{USER_ID}/timelines/reverse_chronological"
    params = {
        "tweet.fields": "author_id,created_at",
        "expansions": "author_id",
        "user.fields": "name,username",
        "max_results": max_results
    }
    if since_id:
        params["since_id"] = since_id

    data = call_twitter_api("GET", url, params=params)
    if "data" not in data:
        return [], {}

    tweets_data = data["data"]  # list of tweets
    includes = data.get("includes", {})
    users_includes = includes.get("users", [])

    # Build a dictionary: {user_id: user_object}
    users_map = {}
    for u in users_includes:
        users_map[u["id"]] = u

    return tweets_data, users_map

def handle_new_tweet(tweet_data, display_name):
    """
    Decides whether to respond to a tweet and sends a reply if needed.
    This is fully blocking due to the AI calls.
    """
    tweet_id = tweet_data["id"]
    if tweet_id in processed_tweet_ids:
        return  # already processed

    processed_tweet_ids.add(tweet_id)
    ogTweet = tweet_data["text"]

    print(f"[handle_new_tweet] New Tweet by {display_name}: {ogTweet}")
    payload = {
        "type": "shouldReply",
        "ogTweet": ogTweet,
        "ogAuthor": display_name
    }
    decision = execute_payload(payload)
    print(f"Decision: {decision}")

    if decision == "WILL_RESPOND":
        reply_payload = {
            "type": "reply",
            "ogTweet": ogTweet,
            "ogAuthor": display_name
        }
        reply_content = execute_payload(reply_payload)
        print(f"Reply: {reply_content}")

        post_tweet(reply_content, in_reply_to_id=tweet_id)

def poll_for_new_tweets_once():
    """
    Performs one polling cycle (fetch timeline, process new tweets).
    """
    global last_seen_id

    tweets, users_map = fetch_home_timeline(since_id=last_seen_id, max_results=10)
    if not tweets:
        return

    # tweets come newest-first; reverse to process oldest-first
    tweets.reverse()

    for t in tweets:
        created_str = t.get("created_at")
        if created_str:
            created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            # skip tweets older than script start
            if created_dt < script_start_time:
                continue

        author_id = t["author_id"]
        
        # 1) IGNORE IF IT'S OUR OWN TWEET
        if str(author_id) == str(USER_ID):
            # It's from us, skip!
            continue

        author_info = users_map.get(author_id, {})
        author_name = author_info.get("name", author_id)

        handle_new_tweet(t, author_name)

        tid_int = int(t["id"])
        if last_seen_id is None or tid_int > int(last_seen_id or 0):
            last_seen_id = str(tid_int)

# ====================
# POSTING TWEETS
# ====================
def post_tweet(content, in_reply_to_id=None):
    """
    Posts a tweet or reply using POST /2/tweets. (Blocking)
    """
    url = "https://api.twitter.com/2/tweets"
    json_body = {"text": content}
    if in_reply_to_id:
        json_body["reply"] = {"in_reply_to_tweet_id": in_reply_to_id}

    try:
        resp_data = call_twitter_api("POST", url, json_body=json_body)
        print(f"Tweet posted! Tweet ID: {resp_data['data']['id']}")
    except Exception as e:
        print(f"Failed to post tweet: {e}")

def post_random_tweet():
    """
    Uses AI to generate a tweet and posts it (Blocking).
    """
    payload = {"type": "tweet"}
    tweet_content = execute_payload(payload)
    print(f"Posting random tweet: {tweet_content}")
    post_tweet(tweet_content)

def main_loop():
    """
    Runs forever, polling tweets and occasionally posting random tweets.
    Because everything is blocking, a long AI call will pause other tasks.
    """
    last_random_post_time = time.time()

    while True:
        try:
            # 1) Poll once
            poll_for_new_tweets_once()

            # 2) Check if time to post random tweet
            now = time.time()
            # If it's been at least a random interval (10-60 min) since last random tweet
            # we can choose a new random interval each cycle or re-check
            if (now - last_random_post_time) >= random.randint(600, 3600):
                post_random_tweet()
                last_random_post_time = time.time()

        except Exception as e:
            print(f"Error in main loop: {e}")

        # Sleep 30s before next poll
        time.sleep(30)

if __name__ == "__main__":
    # 1) Load tokens from file
    try:
        ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_ID, CLIENT_SECRET = load_tokens()
    except Exception as e:
        print(f"Error loading token file {TOKENS_FILE}: {e}")
        exit(1)

    # 2) Retrieve user ID
    try:
        USER_ID = get_my_user_id()
        print(f"User ID: {USER_ID}")
    except Exception as exc:
        print(f"Could not retrieve user ID: {exc}")
        exit(1)

    # 3) Enter main loop (blocks forever)
    main_loop()