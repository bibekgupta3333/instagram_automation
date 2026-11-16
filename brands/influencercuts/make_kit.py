"""InfluencerCuts kit from concept B ('The Cut Creator'): avatar, logo, banner, thumbnail template, watermark."""
from PIL import Image, ImageDraw, ImageFont, ImageFilter
NAVY=(18,23,64); AMBER=(255,200,46); CREAM=(255,246,226)
FB="/System/Library/Fonts/Supplemental/Arial Black.ttf"; FD="/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf"
src="concepts/b-negative-space/"
av=Image.open(src+"avatar-800.png").convert("RGB"); av.save("avatar-800.png")
Image.open(src+"logo-transparent-1024.png").save("logo-transparent-1024.png")
# banner 2560x1440, lockup inside the 1546x423 safe area
W,H=2560,1440; b=Image.new("RGB",(W,H),NAVY); d=ImageDraw.Draw(b)
for k in range(-4,14):                                   # faint diagonal cut bands, same angle family as the mark
    x=k*260; d.polygon([(x,H),(x+90,H),(x+90+580,0),(x+580,0)],fill=(22,28,76))
m=av.resize((400,400),Image.LANCZOS)
f1=ImageFont.truetype(FB,120); f2=ImageFont.truetype(FD,64)
t1="INFLUENCER"; t2="CUTS"; t3="STREAM MOMENTS, CUT SHORT"
w=max(d.textlength(t1,font=f1),d.textlength(t3,font=f2)); total=400+50+w; x0=int((W-total)/2); y0=H//2-200
d.rectangle((x0,y0,x0+400,y0+400),fill=NAVY); b.paste(m,(x0,y0))
tx=x0+450; d.text((tx,y0+10),t1,font=f1,fill=CREAM); d.text((tx,y0+140),t2,font=f1,fill=AMBER); d.text((tx+4,y0+310),t3,font=f2,fill=CREAM)
b.save("banner-2560x1440.png")
# thumbnail template 1280x720
W,H=1280,720; t=Image.new("RGB",(W,H),NAVY); d=ImageDraw.Draw(t)
d.polygon([(0,H),(0,H-190),(W,H-330),(W,H)],fill=AMBER)                    # amber cut wedge for the headline
d.rounded_rectangle((60,110,W-60,H-250),radius=22,fill=(10,13,40),outline=CREAM,width=6)   # picture slot
t.paste(av.resize((84,84),Image.LANCZOS),(60,14)); d.text((160,30),"INFLUENCER CUTS",font=ImageFont.truetype(FB,38),fill=CREAM)
d.text((60,H-150),"YOUR HEADLINE HERE",font=ImageFont.truetype(FB,84),fill=NAVY)
t.save("thumbnail-template-1280x720.png")
# watermark: circular crop of the avatar, 150 px
S=600; mk=Image.new("L",(S,S),0); ImageDraw.Draw(mk).ellipse((0,0,S-1,S-1),fill=255)
wm=av.resize((S,S),Image.LANCZOS).convert("RGBA"); wm.putalpha(mk); wm.resize((150,150),Image.LANCZOS).save("watermark-150.png")
# preview
p=Image.new("RGB",(360+640+640+20,360),(70,70,70)); p.paste(av.resize((360,360)),(0,0)); p.paste(b.resize((640,360)),(370,0)); p.paste(t.resize((640,360)),(1020,0)); p.save("compare.png")
print("ok")
