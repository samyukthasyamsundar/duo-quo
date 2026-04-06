from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text, Float
from sqlalchemy.orm import relationship
from database import Base
import datetime

class User(Base):
    __tablename__ = "users"
    id         = Column(Integer, primary_key=True, index=True)
    email      = Column(String, unique=True, index=True, nullable=False)
    username   = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    posts = relationship("Post", back_populates="author")

class OTPCode(Base):
    __tablename__ = "otp_codes"
    id         = Column(Integer, primary_key=True, index=True)
    email      = Column(String, nullable=False, index=True)
    code       = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used       = Column(Boolean, default=False)

class Post(Base):
    __tablename__ = "posts"
    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    category   = Column(String, nullable=False)
    content    = Column(String, nullable=False)
    latitude   = Column(Float, nullable=True)
    longitude  = Column(Float, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    is_active  = Column(Boolean, default=True)

    author      = relationship("User", back_populates="posts")
    connections = relationship("Connection", back_populates="post")

class Connection(Base):
    __tablename__ = "connections"
    id         = Column(Integer, primary_key=True, index=True)
    post_id    = Column(Integer, ForeignKey("posts.id"), nullable=False)
    joiner_id  = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    post     = relationship("Post", back_populates="connections")
    joiner   = relationship("User", foreign_keys=[joiner_id])
    messages = relationship("Message", back_populates="connection", order_by="Message.created_at")

class Message(Base):
    __tablename__ = "messages"
    id            = Column(Integer, primary_key=True, index=True)
    connection_id = Column(Integer, ForeignKey("connections.id"), nullable=False)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=False)
    content       = Column(Text, nullable=False)
    created_at    = Column(DateTime, default=datetime.datetime.utcnow)

    connection = relationship("Connection", back_populates="messages")
    sender     = relationship("User")
