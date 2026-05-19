from crewai import Agent, Task, Crew,LLM,Process
from langchain_groq import ChatGroq
from crewai_tools import MCPServerAdapter
from dotenv import load_dotenv
import os
import webbrowser
import requests

load_dotenv()


os.environ["AZURE_API_KEY"] = "" # "your-azure-api-key"
os.environ["AZURE_API_BASE"] = "" # "https://your-endpoint.openai.azure.com"
os.environ["AZURE_API_VERSION"] = "" # "2023-05-15"

llm = LLM(
    model="azure/gpt-4o",
)

MCP_URL = "http://127.0.0.1:8000/mcp"

server_params = {
    "url": MCP_URL,
    "transport": "streamable-http",
}


def is_token_valid() -> bool:
    """Check token validity via the server's status endpoint."""
    try:
        resp = requests.get("http://127.0.0.1:8000/health/token-status", timeout=5)
        data = resp.json()
        return data.get("valid", False)
    except Exception:
        return False


def call_mcp_tool(tool_name: str, **kwargs) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": kwargs,
        },
    }
    resp = requests.post(
        MCP_URL,
        json=payload,
        timeout=15,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",  # ← required
        },
    )
    resp.raise_for_status()
    return resp.json()

BASE = "http://127.0.0.1:8000"

def run_oauth_flow():
    print("🔐 No valid token found. Starting OAuth flow...\n")

    # Step 1: Get auth URL via plain HTTP
    resp = requests.get(f"{BASE}/auth/url", timeout=5)
    resp.raise_for_status()
    url = resp.json()["url"]

    print(f"Opening browser for Google authorization...")
    print(f"URL:\n{url}\n")
    webbrowser.open(url)

    print("⚠️  Complete authorization, then immediately paste the redirect URL.")
    redirect_url = input("Paste the full redirect URL here: ").strip()

    # Step 2: Exchange via plain HTTP
    resp = requests.post(
        f"{BASE}/auth/exchange",
        json={"authorization_code": redirect_url},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"\n✅ {data['message']}\n")



def main():
    if not is_token_valid():
        run_oauth_flow()
        if not is_token_valid():
            print("❌ OAuth failed. Please try again.")
            return
        print("✅ Authentication successful!\n")
    else:
        print("✅ Token already valid, skipping OAuth.\n")

    with MCPServerAdapter(server_params) as tools:
        agent = Agent(
            role="Health Data Assistant",
            goal="Fetch and present health data clearly and completely to the user.",
            backstory=(
                "You are an expert health data assistant. When asked for health data, "
                "you ALWAYS call the appropriate tool, wait for the result, and then "
                "present the full data to the user in a readable format. "
                "Never stop at just calling the tool — always show the actual results."
            ),
            tools=tools,
            llm=llm,
            verbose=True,   # ← turn this ON so you can see what's happening
            max_iter=15,
            max_retry_limit=3,
        )

        print("Chat started! Type 'exit' or 'quit' to end.\n")

        while True:
            import sys
            import time
            sys.stdout.flush()
            time.sleep(0.3)
            user_input = input("You: ").strip()

            if user_input.lower() in ["exit", "quit", "bye"]:
                print("\nGoodbye!")
                break

            if not user_input:
                continue

            print("\nAgent is thinking...\n")

            task = Task(
                description=f"The user asks: {user_input}",
                expected_output=(
                    "The actual health data retrieved from the tool, formatted clearly. "
                    "Must include the real values returned by the API, not just a description "
                    "of what tool was called."
                ),
                agent=agent,
            )

            crew = Crew(
                agents=[agent],
                tasks=[task],
                verbose=True,   # ← see full output
                process=Process.sequential,
            )

            result = crew.kickoff()
            print(f"\nAgent: {result}\n")

if __name__ == "__main__":
    main()
