"""MovieRollson brand art: avatar 800x800, transparent logo, banner 2560x1440 (original film-reel mark, PIL, 4x supersampled)."""
import math
from PIL import Image, ImageDraw, ImageFont, ImageFilter
BG=(14,14,20); AMBER=(255,176,32); RED=(226,48,56); CREAM=(250,243,230); DARK=(30,30,40)
FB="/System/Library/Fonts/Supplemental/Arial Black.ttf"; FI="/System/Library/Fonts/Supplemental/Impact.ttf"
def reel(size, bg=True):
    S=size*4; im=Image.new("RGBA",(S,S),BG+(255,) if bg else (0,0,0,0)); d=ImageDraw.Draw(im); c=S/2
    R=S*0.40
    d.ellipse((c-R,c-R,c+R,c+R),fill=AMBER)                      # reel body
    d.ellipse((c-R*0.93,c-R*0.93,c+R*0.93,c+R*0.93),outline=(200,128,10),width=int(S*0.006))
    for k in range(6):                                          # six film holes
        a=math.radians(k*60+30); x=c+math.cos(a)*R*0.60; y=c+math.sin(a)*R*0.60; r=R*0.17
        d.ellipse((x-r,y-r,x+r,y+r),fill=BG if bg else (14,14,20,255))
    r=R*0.30; d.ellipse((c-r,c-r,c+r,c+r),fill=RED)               # hub
    t=R*0.17; d.polygon([(c-t*0.55,c-t),(c-t*0.55,c+t),(c+t*1.0,c)],fill=CREAM)   # play triangle
    # rolling film strip arcing from the reel
    sw=S*0.052; pts=[]
    for i in range(0,40):
        u=i/39; x=c+R*0.55+u*S*0.52*0.9; y=c+R*0.95-math.sin(u*math.pi*0.9)*S*0.30
        pts.append((x,y))
    return im,d,pts,sw
def avatar():
    im,d,_,_=reel(800)
    return im.resize((800,800),Image.LANCZOS).convert("RGB")
def logo():
    im,_,_,_=reel(1024,bg=False); return im.resize((1024,1024),Image.LANCZOS)
def banner():
    W,H=2560,1440; im=Image.new("RGB",(W,H),BG); d=ImageDraw.Draw(im)
    for y in range(H):                                           # soft vignette gradient
        for x in (0,):
            pass
    g=Image.new("L",(W,H),0); gd=ImageDraw.Draw(g); gd.ellipse((W*0.15,H*-0.2,W*0.85,H*1.2),fill=70); g=g.filter(ImageFilter.GaussianBlur(220))
    im.paste(Image.new("RGB",(W,H),(40,26,10)),(0,0),g)
    d=ImageDraw.Draw(im)
    for y0 in (70,H-130):                                         # film strips top and bottom
        d.rectangle((0,y0,W,y0+60),fill=DARK)
        for x in range(30,W,90): d.rounded_rectangle((x,y0+14,x+44,y0+46),radius=7,fill=BG)
    mark=reel(300,bg=False)[0].resize((300,300),Image.LANCZOS)
    f=ImageFont.truetype(FB,128); t="MOVIE ROLLSON"; w=d.textlength(t,font=f)
    f2=ImageFont.truetype(FI,50); t2="BEST SCENES  \u00b7  ONE CLIP AT A TIME"; w2=d.textlength(t2,font=f2)
    total=300+40+max(w,w2); x0=(W-total)/2; y0=H/2-150
    im.paste(mark,(int(x0),int(y0)),mark)
    d.text((x0+340,y0+30),t,font=f,fill=CREAM); d.text((x0+342,y0+190),t2,font=f2,fill=AMBER)
    return im
avatar().save("avatar-800.png"); logo().save("logo-transparent-1024.png"); banner().save("banner-2560x1440.png"); print("ok")
