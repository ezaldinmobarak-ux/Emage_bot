here
import asyncio
import io
import logging
import os

from google import genai
from google.genai import types
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ------------------------------------------------------------------
# الاعدادات (يفضل وضعها كمتغيرات بيئة بدل كتابتها داخل الكود)
# ------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "ضع_التوكن_هنا")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "ضع_مفتاح_جيميني_هنا")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-image")

# ايدي المستخدمين المسموح لهم (فارغ = الجميع). مثال: "123456789,987654321"
ALLOWED_USERS = {
    int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()
}

# رابط الخدمة العام على ريندر، مثال: https://my-bot.onrender.com
# اذا تركته فارغا يعمل البوت بوضع Polling المحلي
WEBHOOK_BASE_URL = (
    os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
).rstrip("/")
# مسار الويب هوك وكلمة سر (حروف وارقام و _ و - فقط)
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "telegram-webhook")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

MAX_CONCURRENT_JOBS = 3
MAX_IMAGE_BYTES = 15 * 1024 * 1024

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("image_bot")

client = genai.Client(api_key=GEMINI_API_KEY)
job_slots = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

PROMPT_WRAPPER = (
    "Edit the provided image according to the instruction below. "
    "Keep the subject identity, composition, lighting and resolution quality "
    "unless the instruction explicitly asks to change them. "
    "Return the edited image.\n\nInstruction: {prompt}"
)

WELCOME = (
    "أهلا بك في بوت تعديل الصور بالذكاء الاصطناعي\n\n"
    "الاستخدام:\n"
    "1) ارسل صورة مع وصف التعديل في الكابشن، مثال: غير الخلفية الى شاطئ عند الغروب\n"
    "2) او ارسل الصورة بدون كابشن وسأطلب منك الوصف بعدها\n"
    "3) او اعمل رد على صورة بنص يصف التعديل\n\n"
    "نصيحة: لجودة اعلى ارسل الصورة كملف (Send as File) لان تليجرام يضغط الصور العادية.\n"
    "الاوامر: /start و /help و /cancel"
)


# ------------------------------------------------------------------
# دوال مساعدة
# ------------------------------------------------------------------
def is_allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USERS


async def download_image(message) -> tuple[bytes, str] | None:
    """تنزيل الصورة من رسالة (صورة عادية او ملف صورة). ترجع (البايتات, نوع الملف)."""
    if message.photo:
        tg_file = await message.photo[-1].get_file()
        mime = "image/jpeg"
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        if message.document.file_size and message.document.file_size > MAX_IMAGE_BYTES:
            return None
        tg_file = await message.document.get_file()
        mime = message.document.mime_type
    else:
        return None
    data = bytes(await tg_file.download_as_bytearray())
    return data, mime


async def edit_with_gemini(image_bytes: bytes, mime: str, prompt: str):
    """ترسل الصورة والوصف الى Gemini وترجع (صورة_معدلة, نص_مرافق)."""
    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            PROMPT_WRAPPER.format(prompt=prompt),
            types.Part.from_bytes(data=image_bytes, mime_type=mime),
        ],
        config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
    )

    out_image, out_text = None, ""
    candidates = response.candidates or []
    if candidates and candidates[0].content and candidates[0].content.parts:
        for part in candidates[0].content.parts:
            if part.inline_data and part.inline_data.data:
                out_image = part.inline_data.data
            elif part.text:
                out_text += part.text
    return out_image, out_text.strip()


async def process_edit(update: Update, image_bytes: bytes, mime: str, prompt: str):
    message = update.message
    status = await message.reply_text("جاري تعديل الصورة، انتظر قليلا...")
    try:
        async with job_slots:
            await message.chat.send_action(ChatAction.UPLOAD_PHOTO)
            out_image, out_text = await edit_with_gemini(image_bytes, mime, prompt)

        if not out_image:
            reason = out_text or "رفض النموذج الطلب او لم يرجع صورة. جرب وصفا مختلفا."
            await status.edit_text(f"لم يتم انتاج صورة.\n{reason}")
            return

        # ارسال كملف بجودة كاملة
        doc = io.BytesIO(out_image)
        doc.name = "edited.png"
        await message.reply_document(
            document=doc,
            caption=(out_text[:900] if out_text else "تم التعديل"),
        )
        # معاينة سريعة كصورة عادية (قد تفشل لو الحجم كبير، لا مشكلة)
        try:
            preview = io.BytesIO(out_image)
            preview.name = "preview.png"
            await message.reply_photo(photo=preview)
        except Exception:
            logger.info("تعذر ارسال المعاينة، تم ارسال الملف فقط")
        await status.delete()

    except Exception as exc:
        logger.exception("فشل التعديل")
        await status.edit_text(
            "حدث خطأ اثناء التعديل. تاكد من المفتاح والحصة والموديل ثم حاول مرة اخرى.\n"
            f"التفاصيل: {type(exc).__name__}"
        )


# ------------------------------------------------------------------
# المعالجات
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(WELCOME)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    context.user_data.pop("pending", None)
    await update.message.reply_text("تم الالغاء. ارسل صورة جديدة متى شئت.")


async def on_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    message = update.message
    result = await download_image(message)
    if result is None:
        await message.reply_text("الملف غير مدعوم او حجمه اكبر من 15 ميجابايت.")
        return
    image_bytes, mime = result
    prompt = (message.caption or "").strip()

    if prompt:
        context.user_data.pop("pending", None)
        await process_edit(update, image_bytes, mime, prompt)
    else:
        context.user_data["pending"] = (image_bytes, mime)
        await message.reply_text("استلمت الصورة. اكتب الان وصف التعديل المطلوب.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    message = update.message
    prompt = (message.text or "").strip()

    # الحالة 1: المستخدم رد على رسالة فيها صورة
    replied = message.reply_to_message
    if replied and (replied.photo or replied.document):
        result = await download_image(replied)
        if result:
            await process_edit(update, result[0], result[1], prompt)
            return

    # الحالة 2: صورة محفوظة بانتظار الوصف
    pending = context.user_data.pop("pending", None)
    if pending:
        await process_edit(update, pending[0], pending[1], prompt)
        return

    await message.reply_text("ارسل صورة اولا مع وصف التعديل. اكتب /help للتعليمات.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("خطأ غير معالج", exc_info=context.error)


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    if WEBHOOK_BASE_URL:
        # وضع Webhook (للنشر على ريندر): تليجرام يرسل التحديثات الى الرابط
        full_url = f"{WEBHOOK_BASE_URL}/{WEBHOOK_PATH}"
        port = int(os.environ.get("PORT", "10000"))
        logger.info("وضع Webhook على المنفذ %s والرابط %s", port, full_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=WEBHOOK_PATH,
            webhook_url=full_url,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        # وضع Polling (للتجربة على جهازك)
        logger.info("وضع Polling محلي، البوت يعمل الان...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
