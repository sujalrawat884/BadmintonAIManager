import os
import logging
import pandas as pd
from datetime import date, datetime, time, timezone, timedelta
from typing import List, Optional, TypedDict, Annotated
from contextlib import asynccontextmanager
from operator import itemgetter

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field, BeforeValidator
from typing_extensions import Annotated as DocAnnotated

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bson import ObjectId

from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI

from twilio.rest import Client

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BadmintonApp")

apiKey = os.getenv("GOOGLE_API_KEY") # Google API Key (Auto-filled by environment)
if not apiKey:
    raise RuntimeError("GOOGLE_API_KEY is required for the Badminton AI Manager.")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")

MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "badminton_club")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "bookings")

motor_client = AsyncIOMotorClient(MONGODB_URL, tz_aware=True, tzinfo=timezone.utc)
db = motor_client[DB_NAME]
bookings_collection = db[COLLECTION_NAME]

scheduler = AsyncIOScheduler()


PyObjectId = DocAnnotated[str, BeforeValidator(str)]

class Booking(BaseModel):
    user_id: str
    user_name: str
    whatsapp_number: str
    court_name: str
    date: date # YYYY-MM-DD
    is_regular_slot: bool = True

class BookingRecord(Booking):
    id: Optional[PyObjectId] = Field(alias="_id", default=None)



@tool
def get_booking_history(lookback_days: int = 30):
    """Return recent bookings as a CSV string for the LLM to analyze."""
    try:
        if lookback_days <= 0:
            raise ValueError("lookback_days must be a positive integer")

        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

        with MongoClient(MONGODB_URL, tz_aware=True, tzinfo=timezone.utc) as client:
            sync_db = client[DB_NAME]
            cursor = (
                sync_db[COLLECTION_NAME]
                .find({"date": {"$gte": cutoff}})
                .sort("date", -1)
                .limit(200)
            )
            data = list(cursor)
            
            if not data:
                return "No bookings found in database."
            
            cleaned = []
            for doc in data:
                # Convert datetime to string date
                d_val = doc.get("date")
                if isinstance(d_val, datetime):
                    if d_val.tzinfo is None:
                        d_val = d_val.replace(tzinfo=timezone.utc)
                    d_val = d_val.astimezone(timezone.utc).strftime("%Y-%m-%d")

                cleaned.append({
                    "user": doc.get("user_name", "Unknown"),
                    "phone": doc.get("whatsapp_number", "Unknown"),
                    "date": d_val,
                    "day": pd.to_datetime(d_val).day_name() if d_val else "Unknown"
                })
                
            df = pd.DataFrame(cleaned)
            return df.to_csv(index=False)
    except Exception as e:
        return f"Error fetching data: {str(e)}"

@tool
def send_whatsapp_reminder(phone_number: str, message_body: str):
    """Send (or simulate) a WhatsApp reminder using Twilio credentials."""
    logger.info(f"📢 AGENT ACTION: Sending message to {phone_number}")
    
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return f"SIMULATION: Message '{message_body}' sent to {phone_number}"

    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=phone_number,
            body=message_body
        )
        return f"Message sent successfully. SID: {msg.sid}"
    except Exception as e:
        return f"Failed to send message: {str(e)}"

tools = [get_booking_history, send_whatsapp_reminder]
tool_map = {t.name: t for t in tools}

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]

def reasoner_node(state: AgentState):
    
    model = ChatGoogleGenerativeAI(model="gemini-2.5-flash", google_api_key=apiKey)
    model_with_tools = model.bind_tools(tools)
    return {"messages": [model_with_tools.invoke(state["messages"])]}

def tool_node(state: AgentState):
    last_message = state["messages"][-1]
    results = []
    tool_calls = getattr(last_message, "tool_calls", [])
    for t in tool_calls:
        logger.info(f"🔧 Invoking Tool: {t['name']}")
        if t['name'] in tool_map:
            try:
                res = tool_map[t['name']].invoke(t['args'])
                results.append(ToolMessage(tool_call_id=t['id'], name=t['name'], content=str(res)))
            except Exception as e:
                results.append(ToolMessage(tool_call_id=t['id'], name=t['name'], content=f"Error: {str(e)}"))
    return {"messages": results}

def router(state: AgentState):
    last_msg = state["messages"][-1]
    return "tools" if getattr(last_msg, "tool_calls", None) else END


workflow = StateGraph(AgentState)
workflow.add_node("reasoner", reasoner_node)
workflow.add_node("tools", tool_node)
workflow.set_entry_point("reasoner")
workflow.add_conditional_edges("reasoner", router, {"tools": "tools", END: END})
workflow.add_edge("tools", "reasoner")
agent_app = workflow.compile()


# async def find_regular_absentees(target_day: date, lookback_weeks: int = 4, min_sessions: int = 3):
#     """Return regular players for the weekday who missed today's booking."""
#     weekday = target_day.isoweekday()
#     day_start = datetime.combine(target_day, time.min, tzinfo=timezone.utc)
#     day_end = day_start + timedelta(days=1)
#     recent_start = day_start - timedelta(weeks=lookback_weeks)

#     pipeline = [
#         {
#             "$match": {
#                 "date": {"$gte": recent_start, "$lt": day_start},
#                 "is_regular_slot": True,
#             }
#         },
#         {"$addFields": {"weekday": {"$isoDayOfWeek": "$date"}}},
#         {"$match": {"weekday": weekday}},
#         {
#             "$group": {
#                 "_id": "$user_id",
#                 "user_name": {"$first": "$user_name"},
#                 "whatsapp_number": {"$first": "$whatsapp_number"},
#                 "court_name": {"$first": "$court_name"},
#                 "count": {"$sum": 1},
#             }
#         },
#         {"$match": {"count": {"$gte": min_sessions}}},
#     ]

#     regulars = await bookings_collection.aggregate(pipeline).to_list(length=None)
#     absentees = []
#     for player in regulars:
#         has_today = await bookings_collection.find_one(
#             {
#                 "user_id": player["_id"],
#                 "date": {"$gte": day_start, "$lt": day_end},
#             }
#         )
#         if not has_today:
#             absentees.append(player)
#     return absentees


# def notify_absentees(absentees: List[dict], today_str: str, day_name: str):
#     if not absentees:
#         return []
#     results = []
#     reminder_tool = tool_map.get("send_whatsapp_reminder")
#     for player in absentees:
#         message = (
#             f"Hey {player['user_name']}! We missed you on the court today ({day_name}). "
#             "Don't let the streak break! 🏸""URL: https://example.com/bookings"
#         )
#         if reminder_tool:
#             res = reminder_tool.invoke({
#                 "phone_number": player["whatsapp_number"],
#                 "message_body": message,
#             })
#         else:
#             res = "Reminder tool unavailable"
#         logger.info("Reminder result for %s: %s", player["user_name"], res)
#         results.append((player["user_name"], res))
#     return results

async def run_daily_streak_check():
    now_local = datetime.now().astimezone()
    today_str = now_local.strftime("%Y-%m-%d")
    day_name = now_local.strftime("%A")
    
    logger.info(f"⏰ STARTING DAILY STREAK CHECK for {today_str} ({day_name})")

    # absentees = await find_regular_absentees(date.today())
    # if absentees:
    #     notify_absentees(absentees, today_str, day_name)
    # else:
    #     logger.info("No absentees found by deterministic check.")
    
    prompt = f"""
    You are the Badminton Club Manager. Today is {today_str} ({day_name}).
    
    Goal: Identify regular players who missed their session today and remind them.
    
    1. Call `get_booking_history` to see recent bookings.
    2. Analyze the data:
       - Identify players who usually play on {day_name}s (e.g. played last 2-3 {day_name}s).
       - Check if they have a booking for TODAY ({today_str}).
     3. If a regular player missed today, craft a UNIQUE reminder for that player (no copy/paste text):
         - Mention their name, usual weekday/court, and the last date you saw them play (based on the data).
         - Suggest their next opportunity or include a motivational line that fits their pattern.
         - Include the bookings portal link once per message: https://www.royalbadmintonclub.com/book-court
         Then call `send_whatsapp_reminder` with that personalized copy.
    
    If no one missed a streak, just output "No reminders needed."
    """
    
    try:
        result = await agent_app.ainvoke({"messages": [HumanMessage(content=prompt)]})
        logger.info("✅ Daily check complete. Agent response:")
        logger.info(result["messages"][-1].content)
    except Exception as e:
        logger.error(f"❌ Error running daily check: {e}")



@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Application starting up...")
    
    try:
        await motor_client.admin.command('ping')
        logger.info("✅ Connected to MongoDB")
    except Exception as e:
        logger.error(f"❌ MongoDB Connection Failed: {e}")

    scheduler.add_job(run_daily_streak_check, 'cron', hour=22, minute=0)
    scheduler.start()
    logger.info("⏰ Scheduler started (Job set for 22:00 daily)")
    
    yield
 
    logger.info("🛑 Application shutting down...")
    scheduler.shutdown()
    motor_client.close()

app = FastAPI(title="Badminton AI Manager", lifespan=lifespan)

@app.post("/bookings", status_code=status.HTTP_201_CREATED)
async def add_booking(booking: Booking):
    booking_data = booking.dict()
    booking_data["date"] = datetime.combine(booking.date, time.min, tzinfo=timezone.utc)
    
    res = await bookings_collection.insert_one(booking_data)
    return {"id": str(res.inserted_id), "message": "Booking confirmed"}

@app.get("/bookings")
async def get_bookings():
    bookings = await bookings_collection.find().sort("date", -1).to_list(100)
    for b in bookings:
        b["_id"] = str(b["_id"])
    return bookings

@app.post("/admin/trigger-check")
async def manual_trigger():

    scheduler.add_job(
        run_daily_streak_check,
        trigger="date",
        run_date=datetime.now(timezone.utc)
    )
    return {"message": "Agent execution triggered in background."}

@app.get("/")
async def root():
    jobs = scheduler.get_jobs()
    next_run = "None"
    if jobs:
        next_time = jobs[0].next_run_time
        next_run = next_time.isoformat() if next_time else "None"
    return {
        "status": "online",
        "scheduler": "running" if scheduler.running else "stopped",
        "next_run": next_run
    }

# To run: uvicorn main:app --reload